"""每個人自己的 API 翻譯金鑰(第 5 階段)。

- 用 .env 的 WEB_KEY_SECRET(主金鑰)加密後存進資料庫;主金鑰不在程式碼、也不在資料庫。
- 畫面只看得到末四碼;API 永遠不回傳金鑰本身。
- 只在工作開始時解密,交給那個工作程序(runner.py),不寫進工作設定、日誌或處理報告。
- 存之前先向 Anthropic / OpenAI 的「列出模型」端點確認金鑰有效(不產生翻譯費用)。
- OpenAI 只連官方網址:讓使用者自訂網址,等於讓他叫伺服器去連任意位址(可能被拿來探測內部網路)。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from app.web.config import Config
from app.web.db import DB

log = logging.getLogger("web.apikeys")

PROVIDERS = {"anthropic": "Claude(Anthropic)", "openai": "OpenAI"}
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,79}$")
KEY_RE = re.compile(r"^[\x21-\x7e]{20,300}$")        # 可見的 ASCII 字元,不含空白
ANTHROPIC_MODELS = "https://api.anthropic.com/v1/models?limit=1"
OPENAI_MODEL = "https://api.openai.com/v1/models/{model}"


class ApiKeyError(ValueError):
    """可以直接顯示給使用者的錯誤。"""


def _fernet(cfg: Config):
    from cryptography.fernet import Fernet

    raw = hashlib.sha256(("subtitle-web/api-keys:" + cfg.key_secret).encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt(cfg: Config, plain: str) -> str:
    return _fernet(cfg).encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(cfg: Config, token: str) -> str | None:
    """解不開(主金鑰換過、資料損壞)時回傳 None,呼叫端當作「沒有金鑰」。"""
    from cryptography.fernet import InvalidToken

    try:
        return _fernet(cfg).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


# ---------------------------------------------------------------------- 驗證
def _get(url: str, headers: dict) -> int:
    req = urllib.request.Request(url, headers={"User-Agent": "subtitle-generator-web", **headers})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def verify(provider: str, key: str, model: str = "") -> None:
    """金鑰無效就丟 ApiKeyError。測試會把這個函式換掉。"""
    name = PROVIDERS[provider]
    try:
        if provider == "anthropic":
            code = _get(ANTHROPIC_MODELS, {"x-api-key": key, "anthropic-version": "2023-06-01"})
        else:
            code = _get(OPENAI_MODEL.format(model=urllib.parse.quote(model, safe="")),
                        {"Authorization": f"Bearer {key}"})
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("無法連到 %s 確認金鑰:%s", provider, getattr(e, "reason", e))
        raise ApiKeyError(f"暫時無法連到 {name} 確認金鑰,請稍後再試(伺服器需要能連到外部網路)") from None
    if code in (401, 403):
        raise ApiKeyError(f"{name} 回覆金鑰無效或沒有權限,請確認金鑰是否複製完整")
    if code == 404 and provider == "openai":
        raise ApiKeyError(f"OpenAI 找不到模型「{model}」,請確認模型名稱(例如 gpt-4o-mini)")
    if code == 429:          # 金鑰有效,只是暫時被限流
        return
    if not 200 <= code < 300:
        raise ApiKeyError(f"{name} 暫時無法確認金鑰(狀態碼 {code}),請稍後再試")


# ---------------------------------------------------------------------- 存取
def clean_input(provider: str, key: str | None, model: str | None) -> tuple[str | None, str]:
    if provider not in PROVIDERS:
        raise ApiKeyError("不支援的 API")
    key = (key or "").strip() or None
    model = (model or "").strip()
    if key is not None and not KEY_RE.match(key):
        raise ApiKeyError("金鑰格式不對:請貼上完整的金鑰(不含空白)")
    if provider == "openai":
        if not model:
            raise ApiKeyError("請填 OpenAI 的模型名稱,例如 gpt-4o-mini")
        if not MODEL_RE.match(model):
            raise ApiKeyError("模型名稱只能有英數字與 . _ : -")
    else:
        model = ""
    return key, model


def save(cfg: Config, db: DB, user_id: int, provider: str, key: str | None, model: str) -> dict:
    """key=None 表示只改模型名稱(金鑰沿用已存的)。"""
    old = db.get_api_keys(user_id).get(provider)
    if key is None:
        if old is None:
            raise ApiKeyError("請貼上金鑰")
        key = decrypt(cfg, old["enc"])
        if key is None:
            raise ApiKeyError("原本的金鑰已無法使用,請重新貼上金鑰")
    if cfg.key_check:
        verify(provider, key, model)
    db.set_api_key(user_id, provider, encrypt(cfg, key), key[-4:], model)
    log.info("使用者 %s 更新了 %s 金鑰", user_id, provider)
    return {**view(db, user_id)[provider], "checked": cfg.key_check}


def view(db: DB, user_id: int) -> dict:
    """給畫面看的資料:有沒有設定、末四碼、模型名稱、更新時間。不含金鑰。"""
    rows = db.get_api_keys(user_id)
    out = {}
    for p, label in PROVIDERS.items():
        r = rows.get(p)
        out[p] = {"label": label, "set": r is not None, "last4": r["last4"] if r else "",
                  "model": r["model"] if r else "", "updated_at": r["updated_at"] if r else None}
    return out


def for_job(cfg: Config, db: DB, user_id: int, provider: str) -> tuple[str, str] | None:
    """工作開始時用:(金鑰, 模型)。沒有或解不開就是 None。"""
    r = db.get_api_keys(user_id).get(provider)
    if r is None:
        return None
    key = decrypt(cfg, r["enc"])
    return (key, r["model"]) if key else None
