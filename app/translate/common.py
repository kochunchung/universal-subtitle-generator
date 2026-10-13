"""翻譯共用工具:語言代碼、跨條句子合併與拆回、譯文換行、檔名規則。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.core.segmenter import Cue

# 介面/檔名用的語言代碼 -> 顯示名稱
LANG_NAMES = {
    "zh-TW": "繁體中文", "zh": "中文", "en": "英文", "ja": "日文", "ko": "韓文",
    "fr": "法文", "de": "德文", "es": "西班牙文", "vi": "越南文", "th": "泰文", "id": "印尼文",
}
# 給 API 模型看的英文語言名稱
LANG_PROMPT_NAMES = {
    "zh-TW": "Traditional Chinese as used in Taiwan (台灣繁體中文)",
    "zh": "Chinese", "en": "English", "ja": "Japanese", "ko": "Korean",
    "fr": "French", "de": "German", "es": "Spanish", "vi": "Vietnamese", "th": "Thai", "id": "Indonesian",
}
TARGETS = ["zh-TW", "en", "ja"]

CJK = {"zh", "zh-TW", "ja", "ko", "yue"}
SENT_END = re.compile(r"[。！？!?…]|[.;](?=[\"')」』]*$)")


def base_lang(lang: str | None) -> str:
    return (lang or "").split("-")[0].lower()


def same_language(a: str | None, b: str | None) -> bool:
    return base_lang(a) == base_lang(b)


def detect_text_lang(text: str) -> str:
    """沒有語言標記的 SRT,用字元種類粗略判斷。"""
    kana = len(re.findall(r"[぀-ヿ]", text))
    han = len(re.findall(r"[一-鿿]", text))
    hangul = len(re.findall(r"[가-힯]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if kana > 10:
        return "ja"
    if hangul > han and hangul > 10:
        return "ko"
    if han > latin / 3:
        return "zh"
    return "en"


def srt_stem_and_lang(path: str | Path) -> tuple[Path, str | None]:
    """影片名.zh-TW.srt → (影片名, 'zh-TW');影片名.srt → (影片名, None)。"""
    p = Path(path)
    name = p.name[:-4] if p.name.lower().endswith(".srt") else p.stem
    m = re.match(r"^(?P<stem>.+)\.(?P<tag>[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?)$", name)
    if m and (base_lang(m.group("tag")) in LANG_NAMES or m.group("tag").islower()):
        return p.with_name(m.group("stem")), m.group("tag")
    return p.with_name(name), None


def output_path(stem: Path, src: str, tgt: str, bilingual: bool) -> Path:
    tag = f"{src}-{tgt}" if bilingual else tgt
    return Path(f"{stem}.{tag}.srt")


# ---------------------------------------------------------------- 合併與拆回
@dataclass
class Group:
    indices: list[int]
    text: str


def cue_text(c: Cue) -> str:
    """一條字幕的文字(多行合成一行)。"""
    return _join_lines(c.lines)


def _join_lines(lines: list[str]) -> str:
    out = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if out and _needs_space(out[-1], line[0]):
            out += " "
        out += line
    return out


def _needs_space(a: str, b: str) -> bool:
    return bool(re.match(r"[A-Za-z0-9,.;:!?)\"']", a)) and bool(re.match(r"[A-Za-z0-9(\"']", b))


def merge_sentences(cues: list[Cue], max_group: int = 3, max_gap: float = 1.0) -> list[Group]:
    """相鄰字幕若前一條不是句尾、間隔又很短,視為同一句合併翻譯(最多合併 max_group 條)。"""
    groups: list[Group] = []
    for i, c in enumerate(cues):
        t = cue_text(c)
        if groups:
            g = groups[-1]
            prev = cues[g.indices[-1]]
            if (len(g.indices) < max_group and not SENT_END.search(cue_text(prev))
                    and c.start - prev.end <= max_gap):
                g.indices.append(i)
                g.text = _join_lines([g.text, t])
                continue
        groups.append(Group([i], t))
    return groups


def split_by_ratio(text: str, weights: list[int], lang: str) -> list[str]:
    """把一段譯文依原文各條的長度比例切回多段,切點盡量落在標點或詞與詞之間。"""
    n = len(weights)
    if n <= 1:
        return [text.strip()]
    text = text.strip()
    total_w = max(1, sum(weights))
    cuts_ok = _cut_positions(text, lang)
    parts, start, acc = [], 0, 0
    for k in range(n - 1):
        acc += weights[k]
        ideal = round(len(text) * acc / total_w)
        lo, hi = start + 1, len(text) - (n - 1 - k)
        if lo > hi:
            parts.append("")
            continue
        window = max(2, len(text) // (n * 3))
        best, best_score = min(max(ideal, lo), hi), float("inf")
        for pos in range(max(lo, ideal - window), min(hi, ideal + window) + 1):
            if pos not in cuts_ok:
                continue
            bonus = -3 if text[pos - 1] in "，。、,.;；:：!?！？" else 0
            score = abs(pos - ideal) + bonus
            if score < best_score:
                best, best_score = pos, score
        parts.append(text[start:best].strip())
        start = best
    parts.append(text[start:].strip())
    return parts


def _cut_positions(text: str, lang: str) -> set[int]:
    """可以切開的字元位置:中文用分詞邊界;英文等用空白;日文每個字都可以。"""
    if base_lang(lang) == "zh":
        try:
            import rjieba

            out, pos = set(), 0
            for tok in rjieba.cut(text):
                pos += len(tok)
                out.add(pos)
            return out
        except Exception:
            return set(range(1, len(text)))
    if base_lang(lang) in CJK:
        return set(range(1, len(text)))
    return {i + 1 for i, ch in enumerate(text) if ch == " "} | {i for i, ch in enumerate(text) if ch == " "}


def wrap_text(text: str, lang: str, line_max: int | None = None, max_lines: int = 2) -> list[str]:
    """譯文換行:超過一行長度時,在最接近中間、最好是標點的位置分成兩行。"""
    text = text.strip()
    cjk = base_lang(lang) in CJK
    line_max = line_max or (18 if cjk else 42)
    if len(text) <= line_max or max_lines < 2:
        return [text] if text else []
    cuts = _cut_positions(text, lang)
    mid = len(text) / 2
    best, best_score = None, float("inf")
    for pos in cuts:
        if pos <= 0 or pos >= len(text):
            continue
        left, right = len(text[:pos].strip()), len(text[pos:].strip())
        over = max(0, left - line_max) + max(0, right - line_max)
        bonus = -2 if text[pos - 1] in "，、,;；:：" else 0
        score = abs(pos - mid) / max(1, len(text)) * 10 + over * 2 + bonus
        if score < best_score:
            best, best_score = pos, score
    if best is None:
        best = int(mid)
    first, second = text[:best].strip(), text[best:].strip()
    if cjk:
        first = re.sub(r"[，、,]$", "", first)
    return [l for l in (first, second) if l]
