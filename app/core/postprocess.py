"""轉錄後處理:過濾 Whisper 幻覺、壓縮重複字詞、簡轉繁。"""
from __future__ import annotations

import re
import zlib
from functools import lru_cache

from app.core.transcriber import ChunkText, Word

# Whisper 在靜音、音樂或片尾常見的幻覺句(取自訓練資料中的字幕組標語)
HALLUCINATION_PATTERNS = [
    r"請不吝點贊", r"请不吝点赞", r"訂閱.{0,6}轉發", r"订阅.{0,6}转发", r"打賞支持", r"打赏支持",
    r"明鏡與點點", r"明镜与点点", r"字幕由.{0,20}提供", r"字幕[bB][yY]", r"[Aa]mara\.org",
    r"優優獨播", r"优优独播", r"YoYo Television", r"中文字幕志願者", r"中文字幕志愿者",
    r"感謝觀看.{0,4}$", r"谢谢观看.{0,4}$", r"ご視聴ありがとうございました",
    r"^\s*Thank you for watching[.!]?\s*$", r"^\s*Thanks for watching[.!]?\s*$",
    r"^\s*(字幕|翻譯|翻译|校對|校对)[::]",
]
_HALLU = [re.compile(p) for p in HALLUCINATION_PATTERNS]


def compression_ratio(text: str) -> float:
    b = text.encode("utf-8")
    return len(b) / max(1, len(zlib.compress(b)))


def is_hallucination(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    rest = t
    for rx in _HALLU:
        rest = rx.sub("", rest)
    if rest == t:
        return False
    # 只有「整段幾乎都是幻覺句」才刪,避免誤刪正常內容裡剛好出現的詞
    leftover = re.sub(r"[\s\W_]+", "", rest)
    return len(leftover) <= 6


def collapse_repeats(words: list[Word], max_repeat: int = 2, max_n: int = 8) -> list[Word]:
    """同一段字詞連續重複超過 max_repeat 次時,只保留前 max_repeat 次。
    例:「好的好的好的好的好的」→「好的好的」。"""
    out = list(words)
    changed = True
    while changed:
        changed = False
        for n in range(1, max_n + 1):
            i = 0
            while i + n * (max_repeat + 1) <= len(out):
                unit = [w.text.strip() for w in out[i:i + n]]
                k = 1
                while i + n * (k + 1) <= len(out) and [w.text.strip() for w in out[i + n * k:i + n * (k + 1)]] == unit:
                    k += 1
                if k > max_repeat:
                    del out[i + n * max_repeat:i + n * k]
                    changed = True
                i += 1
    return out


def clean_chunk(chunk: ChunkText) -> tuple[ChunkText, str | None]:
    """回傳(清理後的結果, 被移除的原因或 None)。"""
    if is_hallucination(chunk.text):
        return ChunkText("", []), f"幻覺句:{chunk.text[:40]}"
    words = collapse_repeats(chunk.words)
    note = None
    if len(words) < len(chunk.words):
        note = f"移除重複字詞 {len(chunk.words) - len(words)} 個"
    text = "".join(w.text for w in words).strip()
    return ChunkText(text, words), note


@lru_cache(maxsize=4)
def _opencc(config: str):
    from opencc import OpenCC

    return OpenCC(config)


def to_traditional(text: str, config: str = "s2twp") -> str:
    """簡體轉台灣繁體(含用語,例如 软件→軟體)。"""
    return _opencc(config).convert(text)


def to_simplified(text: str) -> str:
    return _opencc("t2s").convert(text)
