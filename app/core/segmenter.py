"""依逐字時間戳重新斷句,產生易讀的字幕條。

規則(可在 settings.json 的 segmenter 調整):
- 中日韓:每行最多 18 字;其他語言:每行最多 42 字元
- 每條最多 2 行、最長 7 秒
- 優先在句末標點、停頓處斷開;必須切開時,選在逗號或停頓、且兩邊長度接近的位置
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.transcriber import Word

CJK_LANGS = {"zh", "ja", "ko", "yue"}
STRONG_END = re.compile(r"[。！？!?…]+[」』”\"')）]*$|(?<![A-Z][a-z])(?<!\b[A-Z])\.[\"')]*$")
WEAK_END = re.compile(r"[，、,;；:：]$")
TRAILING_CJK_PUNCT = re.compile(r"[，、。,]+$")
PAUSE_BREAK_S = 0.8       # 停頓超過這個秒數,視為句子結束
MIN_CUE_S = 0.8           # 字幕至少顯示這麼久(不與下一條重疊的前提下)
LEAD_OUT_S = 0.25         # 說完後多留一點時間


@dataclass
class Cue:
    start: float
    end: float
    lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass
class SegmenterConfig:
    max_chars_cjk: int = 18
    max_chars_latin: int = 42
    max_lines: int = 2
    max_duration: float = 7.0

    @classmethod
    def from_settings(cls, d: dict | None) -> "SegmenterConfig":
        d = d or {}
        return cls(**{k: d[k] for k in ("max_chars_cjk", "max_chars_latin", "max_lines", "max_duration") if k in d})


def _join(words: list[Word]) -> str:
    return "".join(w.text for w in words).strip()


def _chars(words: list[Word]) -> int:
    return len(_join(words))


def _dur(words: list[Word]) -> float:
    return words[-1].end - words[0].start


def _word_boundaries(text: str) -> set[int] | None:
    """用中文分詞找出詞與詞之間的位置(字元偏移量),斷句與換行只在這些位置切開。
    分詞字典以簡體為主,先轉簡體再分詞(轉換前後字數相同時才採用)。沒裝 rjieba 時回傳 None。"""
    try:
        import rjieba

        from app.core.postprocess import to_simplified
    except Exception:
        return None
    probe = to_simplified(text)
    if len(probe) != len(text):
        probe = text
    out, pos = {0}, 0
    for tok in rjieba.cut(probe):
        pos += len(tok)
        out.add(pos)
    return out


def _glued(a: Word, b: Word) -> bool:
    """兩個片段是同一個英文單字或數字(例如 M + ove),不能從中間斷開。"""
    return bool(re.search(r"[A-Za-z0-9]$", a.text)) and bool(re.match(r"^[A-Za-z0-9]", b.text))


class Segmenter:
    def __init__(self, language: str | None, cfg: SegmenterConfig | None = None) -> None:
        self.cfg = cfg or SegmenterConfig()
        self.cjk = (language or "").split("-")[0] in CJK_LANGS
        self.zh = (language or "").split("-")[0] in ("zh", "yue")
        self.line_max = self.cfg.max_chars_cjk if self.cjk else self.cfg.max_chars_latin

    # ---------------------------------------------------------------- 主流程
    def segment(self, words: list[Word]) -> list[Cue]:
        words = [w for w in words if w.text.strip()]
        if not words:
            return []
        cues: list[Cue] = []
        for sent in self._sentences(words):
            for part in self._split_to_fit(sent):
                cues.append(Cue(part[0].start, part[-1].end, self._wrap(part)))
        cues = self._merge_tiny(cues)
        self._fix_timing(cues)
        return [c for c in cues if any(l.strip() for l in c.lines)]

    # ---------------------------------------------------------------- 步驟
    def _sentences(self, words: list[Word]) -> list[list[Word]]:
        out, cur = [], []
        for i, w in enumerate(words):
            cur.append(w)
            nxt = words[i + 1] if i + 1 < len(words) else None
            gap = (nxt.start - w.end) if nxt else 0.0
            if nxt is None or STRONG_END.search(w.text.strip()) or gap >= PAUSE_BREAK_S:
                out.append(cur)
                cur = []
        return out

    def _fits(self, ws: list[Word]) -> bool:
        return _chars(ws) <= self.line_max * self.cfg.max_lines and _dur(ws) <= self.cfg.max_duration

    def _split_to_fit(self, ws: list[Word]) -> list[list[Word]]:
        if len(ws) <= 1 or self._fits(ws):
            return [ws]
        total = _chars(ws)
        best_k, best_score = 1, float("-inf")
        for k in self._candidates(ws):
            left = _chars(ws[:k])
            balance = 1.0 - abs(left - (total - left)) / max(1, total)
            prev = ws[k - 1].text.strip()
            punct = 1.0 if WEAK_END.search(prev) else 0.0
            gap = max(0.0, ws[k].start - ws[k - 1].end)
            score = balance + punct + 0.6 * min(gap, 1.0)
            if score > best_score:
                best_k, best_score = k, score
        return self._split_to_fit(ws[:best_k]) + self._split_to_fit(ws[best_k:])

    def _candidates(self, ws: list[Word]) -> list[int]:
        """可以切開的位置(第 k 個字之前)。不切開英文單字;中文只在分詞邊界切。"""
        ks = [k for k in range(1, len(ws)) if not _glued(ws[k - 1], ws[k])]
        if self.zh and ks:
            text = "".join(w.text for w in ws)
            bounds = _word_boundaries(text)
            if bounds:
                offsets, pos = {}, 0
                for k, w in enumerate(ws):
                    offsets[k] = pos
                    pos += len(w.text)
                good = [k for k in ks if offsets[k] in bounds]
                if good:
                    ks = good
        return ks or list(range(1, len(ws)))

    def _clean(self, text: str) -> str:
        text = text.strip()
        if self.cjk:
            text = TRAILING_CJK_PUNCT.sub("", text)
        return text

    def _wrap(self, ws: list[Word]) -> list[str]:
        """一條字幕超過一行的長度時,在最接近中間、最好是標點的位置分成兩行。"""
        text = self._clean(_join(ws))
        if len(text) <= self.line_max or len(ws) <= 1:
            return [text]
        total = _chars(ws)
        best_k, best_score = 1, float("-inf")
        for k in self._candidates(ws):
            left = _chars(ws[:k])
            right = total - left
            balance = 1.0 - abs(left - right) / max(1, total)
            punct = 1.0 if WEAK_END.search(ws[k - 1].text.strip()) else 0.0
            over = max(0, left - self.line_max) + max(0, right - self.line_max)
            score = balance + punct - 0.5 * over   # 每超出一個字扣分,兩行都不超長優先
            if score > best_score:
                best_k, best_score = k, score
        first = _join(ws[:best_k])
        second = _join(ws[best_k:])
        if self.cjk:
            first = TRAILING_CJK_PUNCT.sub("", first)
        lines = [first.strip(), self._clean(second)]
        return [l for l in lines if l]

    def _merge_tiny(self, cues: list[Cue]) -> list[Cue]:
        """太短的字幕條(例如只有「嗯」)併入前一條。"""
        out: list[Cue] = []
        for c in cues:
            if out:
                p = out[-1]
                short = (c.end - c.start) < 0.5 and len(c.text.replace("\n", "")) <= 3
                close = c.start - p.end < 0.5
                merged_len = len(p.text.replace("\n", "")) + len(c.text.replace("\n", ""))
                if short and close and merged_len <= self.line_max * self.cfg.max_lines \
                        and (c.end - p.start) <= self.cfg.max_duration:
                    sep = "" if self.cjk else " "
                    joined = (p.text.replace("\n", sep) + sep + c.text).strip()
                    p.end = c.end
                    p.lines = self._rewrap_text(joined)
                    continue
            out.append(c)
        return out

    def _rewrap_text(self, text: str) -> list[str]:
        if len(text) <= self.line_max:
            return [text]
        cut = len(text) // 2
        if not self.cjk:
            sp = text.rfind(" ", 0, cut + 1)
            cut = sp if sp > 0 else cut
        return [text[:cut].strip(), text[cut:].strip()]

    def _fix_timing(self, cues: list[Cue]) -> None:
        # 先確保開始時間遞增、不早於前一條的開始
        for i in range(1, len(cues)):
            if cues[i].start < cues[i - 1].start + 0.1:
                cues[i].start = cues[i - 1].start + 0.1
            if cues[i].end < cues[i].start:
                cues[i].end = cues[i].start
        for i, c in enumerate(cues):
            nxt = cues[i + 1].start if i + 1 < len(cues) else None
            limit = (nxt - 0.05) if nxt is not None else c.end + 2.0
            want = max(c.end + LEAD_OUT_S, c.start + MIN_CUE_S)
            c.end = max(c.end, min(want, limit))
            if nxt is not None and c.end > nxt:
                c.end = nxt
            if c.end <= c.start:
                c.end = c.start + 0.1
