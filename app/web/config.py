"""網頁版設定:全部來自環境變數(開發時可放在程式資料夾的 .env)。

金鑰、登入平台的 Client ID 都不寫進程式碼。.env 不進版本控制。
每個人的上限(檔案大小、每天時數、空間等)依會員等級決定,在網頁的「管理」頁設定,見 perms.py。
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from app.paths import ROOT

DEFAULT_PORT = 8780        # 10/8:8000 容易和其他服務衝突,改用較少被佔用的埠號;可在 .env 的 WEB_PORT 改


def _load_dotenv(path: Path) -> None:
    """很小的 .env 讀取器:KEY=VALUE,# 開頭是註解;已存在的環境變數優先。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _list(name: str, default: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in os.environ.get(name, default).split(",") if x.strip())


@dataclass(frozen=True)
class Config:
    data_dir: Path
    host: str
    port: int
    secret_key: str
    dev_login: bool
    engines: tuple[str, ...]            # 網頁版開放的翻譯引擎(NLLB 授權不適用收費服務,預設不開)
    whisper_models: tuple[str, ...]     # 開放的轉錄模型(Hugging Face 代號),第一個是預設
    batch_size: int | None              # 管理者固定的轉錄批次大小;None = 依顯存自動
    tz_offset_hours: float              # 「每天」以哪個時區的午夜歸零(台灣 = 8)
    chunk_bytes: int                    # 分塊上傳每塊的大小
    max_events_per_job: int
    priority_max_wait_s: int            # 一般會員的工作等超過這麼久,就不再被付費會員插隊
    key_secret: str                     # 加密使用者 API 金鑰的主金鑰(不在程式碼、也不在資料庫)
    day_seconds: int                    # 保存天數的「1 天」幾秒;只有驗收時才調小(例如 60 = 1 分鐘)
    key_check: bool                     # 存 API 金鑰前,先向 Anthropic / OpenAI 確認金鑰有效
    speed_x: float                      # 預估等待時間用:處理速度約為音訊長度的幾倍(Teams.mp4 實測約 49 倍)
    worker_target: str | None           # 測試用:"模組:函式",取代真正的 worker.run
    public_url: str                     # 使用者在瀏覽器輸入的網址(Google 登入的回呼網址由它組成),結尾不含 /
    google_client_id: str
    google_client_secret: str
    allowed_domains: tuple[str, ...]    # 只允許這些信箱網域登入;空白 = 不限制(10/8 決定不限制)
    allowed_emails: tuple[str, ...]     # 另外允許的個別信箱(網域限制打開時才有意義)
    require_approval: bool              # 新帳號要管理者在管理頁核准後才能使用(10/8 決定加回審核,預設開啟)
    admin_emails: tuple[str, ...]       # 這些信箱登入後自動成為管理者

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def secure_cookies(self) -> bool:
        """網站用 HTTPS 時,Cookie 只走 HTTPS。"""
        return self.public_url.lower().startswith("https://")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "web.sqlite3"

    def user_dir(self, user_id: int) -> Path:
        return self.data_dir / "users" / str(user_id)

    def item_dir(self, user_id: int, item_id: str) -> Path:
        return self.user_dir(user_id) / "items" / item_id

    def glossary_file(self, user_id: int) -> Path:
        return self.user_dir(user_id) / "glossary.txt"


def _dev_secret(path: Path) -> str:
    if not path.is_file():
        path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


def load() -> Config:
    _load_dotenv(ROOT / ".env")
    data_dir = Path(os.environ.get("WEB_DATA_DIR") or ROOT / "data" / "web").resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    dev = _bool("WEB_DEV_LOGIN", False)
    key = os.environ.get("WEB_SECRET_KEY", "")
    if not key:
        if not dev:
            raise RuntimeError("正式環境必須在 .env 設定 WEB_SECRET_KEY(登入 Cookie 的簽署金鑰)")
        key = _dev_secret(data_dir / "dev-secret.key")   # 開發模式:自動產生並保存,重開機後登入狀態不會失效
    key_secret = os.environ.get("WEB_KEY_SECRET", "").strip()
    if not key_secret:
        if not dev:
            raise RuntimeError("正式環境必須在 .env 設定 WEB_KEY_SECRET(加密使用者 API 金鑰的主金鑰);"
                               "執行 setup-google-login.bat 會自動產生")
        key_secret = _dev_secret(data_dir / "dev-key-secret.key")
    from app.settings import DEFAULT_WHISPER

    return Config(
        data_dir=data_dir,
        host=os.environ.get("WEB_HOST", "127.0.0.1"),
        port=_int("WEB_PORT", DEFAULT_PORT),
        secret_key=key,
        dev_login=dev,
        engines=_list("WEB_ENGINES", "m2m100,anthropic,openai"),
        whisper_models=_list("WEB_WHISPER_MODELS", DEFAULT_WHISPER),
        batch_size=_int("WEB_BATCH_SIZE", 0) or None,
        tz_offset_hours=float(os.environ.get("WEB_TZ_OFFSET_HOURS") or 8),
        chunk_bytes=_int("WEB_CHUNK_MB", 16) * 2**20,
        max_events_per_job=_int("WEB_MAX_EVENTS_PER_JOB", 5000),
        priority_max_wait_s=_int("WEB_PRIORITY_MAX_WAIT_MIN", 30) * 60,
        key_secret=key_secret,
        day_seconds=max(10, _int("WEB_DAY_SECONDS", 86400)),
        key_check=_bool("WEB_KEY_CHECK", True),
        speed_x=float(os.environ.get("WEB_SPEED_X") or 40),
        worker_target=os.environ.get("WEB_WORKER_TARGET") or None,
        public_url=(os.environ.get("WEB_PUBLIC_URL") or f"http://localhost:{_int('WEB_PORT', DEFAULT_PORT)}").rstrip("/"),
        google_client_id=os.environ.get("WEB_GOOGLE_CLIENT_ID", "").strip(),
        google_client_secret=os.environ.get("WEB_GOOGLE_CLIENT_SECRET", "").strip(),
        allowed_domains=tuple(d.lower().lstrip("@") for d in _list("WEB_ALLOWED_EMAIL_DOMAINS", "")),
        allowed_emails=tuple(e.lower() for e in _list("WEB_ALLOWED_EMAILS", "")),
        require_approval=_bool("WEB_REQUIRE_APPROVAL", True),
        admin_emails=tuple(e.lower() for e in _list("WEB_ADMIN_EMAILS", "")),
    )
