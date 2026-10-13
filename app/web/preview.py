"""字幕預覽:讀 SRT,標出疑似有問題的條目。

標示規則照拿桌面版 gui/dialogs.py 的 PreviewDialog(不 import gui,避免伺服器需要 PySide6):
  - 顯示時間不到 0.5 秒
  - 每秒超過 15 個字
  - postprocess.is_hallucination 判定為疑似幻覺句
"""
from __future__ import annotations

from pathlib import Path

MIN_DURATION = 0.5
MAX_CHARS_PER_SEC = 15


def preview(path: str | Path) -> dict:
    from app.core import postprocess, srt_io

    cues = srt_io.read_srt(path)
    out = []
    flagged = 0
    for i, c in enumerate(cues):
        text = c.text
        flat = text.replace("\n", "")
        dur = max(0.01, c.end - c.start)
        reasons = []
        if dur < MIN_DURATION:
            reasons.append("顯示時間很短")
        if len(flat) / dur > MAX_CHARS_PER_SEC:
            reasons.append("字數相對時間過多")
        if postprocess.is_hallucination(flat):
            reasons.append("疑似幻覺句")
        flagged += bool(reasons)
        out.append({"n": i + 1, "start": srt_io.format_ts(c.start), "end": srt_io.format_ts(c.end),
                    "text": text, "flags": reasons})
    return {"cues": out, "total": len(out), "flagged": flagged}
