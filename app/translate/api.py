"""API 翻譯:Anthropic(Claude)或 OpenAI 相容服務(含 Azure OpenAI、公司自架服務)。

- 逐條翻譯,每條帶編號;一次送 30 條,另附前後各 5 條當上下文(只供參考,不翻譯)
- 要求模型只回傳 JSON:{"編號": "譯文", ...};跨條的句子由模型依語意分配到各條
- 漏掉的編號只重送那幾條;重試兩次仍缺的改為逐條翻譯;還是失敗就保留原文並記錄
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Callable

from app.translate import keys
from app.translate.common import LANG_PROMPT_NAMES, base_lang

log = logging.getLogger(__name__)

BATCH = 30
CONTEXT = 5
MAX_TOKENS = 8000

SYSTEM_PROMPT = """You are a professional subtitle translator.
Translate each subtitle line into {target}.
Rules:
- Return ONLY a JSON object mapping every given id (as a string) to its translation. No markdown, no comments.
- Translate every id exactly once. Never merge, skip, reorder or add ids.
- A sentence may be split across consecutive ids. Translate naturally and distribute the meaning across
  those ids in reading order, so each line still matches its own timing.
- Keep translations concise and natural for on-screen subtitles. Do not add explanations.
- Lines in "context_before" and "context_after" are for understanding only. Do not translate them.
{glossary}"""


class ApiError(RuntimeError):
    pass


class ApiTranslator:
    kind = "api"

    def __init__(self, provider: str, settings: dict) -> None:
        if provider not in keys.PROVIDERS:
            raise ApiError(f"不支援的 API:{provider}")
        # 網頁版:每個人自己的金鑰由工作程序的環境變數帶進來;沒有才用 Windows 認證管理員(桌面版行為不變)
        key = os.environ.get(f"APP_API_KEY_{provider.upper()}") or keys.get_key(provider)
        if not key:
            raise ApiError(f"尚未設定 {provider} 的 API 金鑰,請先執行 set-api-key.bat")
        self.provider = provider
        self.notes: list[str] = []
        api = settings.get("api") or {}
        if provider == "anthropic":
            import anthropic

            self.client = anthropic.Anthropic(api_key=key, max_retries=4, timeout=120)
            self.model = api.get("anthropic_model") or self._pick_claude_model()
        else:
            import openai

            self.client = openai.OpenAI(
                api_key=key, base_url=api.get("openai_base_url") or None, max_retries=4, timeout=120)
            self.model = api.get("openai_model") or ""
            if not self.model:
                raise ApiError("尚未設定 OpenAI 相容 API 的模型名稱,請先執行 set-api-key.bat")
        log.info("API 翻譯:%s,模型 %s", provider, self.model)

    def _pick_claude_model(self) -> str:
        """沒有指定模型時,自動選帳號可用的最新 Sonnet 模型。"""
        try:
            models = [m.id for m in self.client.models.list(limit=50)]
        except Exception as e:
            raise ApiError(f"無法取得 Claude 模型清單,請確認金鑰與網路:{e}") from e
        for m in models:
            if "sonnet" in m:
                return m
        if models:
            return models[0]
        raise ApiError("帳號沒有可用的 Claude 模型")

    # ---------------------------------------------------------------- 呼叫
    def _call(self, system: str, user: str) -> str:
        if self.provider == "anthropic":
            r = self.client.messages.create(
                model=self.model, max_tokens=MAX_TOKENS, system=system,
                messages=[{"role": "user", "content": user}],
            )
            return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        r = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return r.choices[0].message.content or ""

    # ---------------------------------------------------------------- 翻譯
    def translate_lines(self, lines: list[str], src: str, tgt: str, pairs: list[tuple[str, str]],
                        on_progress: Callable[[int, int], None] | None = None) -> list[str | None]:
        system = SYSTEM_PROMPT.format(
            target=LANG_PROMPT_NAMES.get(tgt, LANG_PROMPT_NAMES.get(base_lang(tgt), tgt)),
            glossary=_glossary_block(pairs, lines),
        )
        out: list[str | None] = [None] * len(lines)
        for start in range(0, len(lines), BATCH):
            ids = list(range(start, min(len(lines), start + BATCH)))
            self._translate_ids(ids, lines, system, out)
            if on_progress:
                on_progress(ids[-1] + 1, len(lines))
        missing = [i for i, t in enumerate(out) if not t]
        if missing:
            self.notes.append(f"{len(missing)} 條字幕翻譯失敗,保留原文")
        return out

    def _translate_ids(self, ids: list[int], lines: list[str], system: str, out: list) -> None:
        todo = list(ids)
        for attempt in range(3):
            if not todo:
                return
            got = self._request(todo, lines, system)
            for i in todo:
                if got.get(i):
                    out[i] = got[i]
            todo = [i for i in todo if not out[i]]
            if todo:
                log.info("第 %d 次:%d 條沒有拿到譯文,重送", attempt + 1, len(todo))
        for i in todo:   # 最後逐條處理
            got = self._request([i], lines, system)
            if got.get(i):
                out[i] = got[i]

    def _request(self, ids: list[int], lines: list[str], system: str) -> dict[int, str]:
        lo, hi = ids[0], ids[-1]
        payload = {
            "context_before": lines[max(0, lo - CONTEXT):lo],
            "lines": [{"id": str(i), "text": lines[i]} for i in ids],
            "context_after": lines[hi + 1:hi + 1 + CONTEXT],
        }
        try:
            text = self._call(system, json.dumps(payload, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            fatal = _fatal_reason(e)
            if fatal:   # 金鑰錯誤、沒有權限、模型不存在:重試也沒用,直接停止
                raise ApiError(fatal) from e
            log.warning("API 呼叫失敗:%s", e)
            msg = f"API 呼叫失敗:{e.__class__.__name__}"
            if msg not in self.notes:
                self.notes.append(msg)
            return {}
        return parse_json_map(text, set(ids))


def _fatal_reason(e: Exception) -> str | None:
    status = getattr(e, "status_code", None)
    name = e.__class__.__name__
    if status == 401 or name == "AuthenticationError":
        return ("API 金鑰無效(401)。請重新執行 set-api-key.bat 確認金鑰;"
                "如果金鑰不是向 OpenAI 官方申請的(例如公司或第三方服務),要同時填入服務網址")
    if status == 403 or name == "PermissionDeniedError":
        return "這把金鑰沒有使用權限(403),請確認帳號已開通 API 或有剩餘額度"
    if status == 404 or name == "NotFoundError":
        return "找不到指定的模型或服務網址(404),請重新執行 set-api-key.bat 確認模型名稱與網址"
    return None


def parse_json_map(text: str, wanted: set[int]) -> dict[int, str]:
    """從回應中取出 JSON 物件,轉成 {編號: 譯文};容忍前後多餘文字與 ```json 包裝。"""
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b <= a:
        return {}
    try:
        data = json.loads(s[a:b + 1])
    except json.JSONDecodeError:
        return {}
    out = {}
    if isinstance(data, dict):
        for k, v in data.items():
            try:
                i = int(str(k).strip())
            except ValueError:
                continue
            if i in wanted and isinstance(v, str) and v.strip():
                out[i] = v.strip()
    return out


def _glossary_block(pairs: list[tuple[str, str]], lines: list[str]) -> str:
    """只放這份字幕裡真的出現的術語,避免提示詞過長。"""
    joined = "\n".join(lines)
    used = [(s, d) for s, d in pairs if s and d and s in joined]
    if not used:
        return ""
    body = "\n".join(f"- {s} => {d}" for s, d in used[:200])
    return "- Always use these fixed translations for the following terms:\n" + body
