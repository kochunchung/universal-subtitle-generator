"""找出一部影片「已經轉錄好的原文字幕」,讓 GUI 可以直接翻譯、不必重新轉錄。

1. 先看 logs\\reports 裡這部影片最近一次的處理報告(報告記錄了原文字幕的路徑)
2. 沒有報告時(例如日誌被清掉),影片旁邊只有一個「影片名.語言.srt」就當作原文字幕;
   有好幾個(原文 + 譯文)就無法判斷,回傳 None
"""
from __future__ import annotations

import glob
import json
import os
from pathlib import Path

from app.paths import LOGS_DIR


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def find_transcript(media: str | Path) -> Path | None:
    media = Path(media)
    hit = _from_reports(media)
    if hit is not None:
        return hit
    return _from_siblings(media)


def _from_reports(media: Path) -> Path | None:
    d = LOGS_DIR / "reports"
    if not d.is_dir():
        return None
    target = _norm(media)
    names = glob.glob("*_" + glob.escape(media.stem) + ".json", root_dir=d)
    for name in sorted(names, reverse=True):          # 檔名開頭是時間,新的在前
        try:
            data = json.loads((d / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if _norm(data.get("source", "")) != target or data.get("status") not in ("done", "skipped"):
            continue
        srt = (data.get("outputs") or {}).get("srt")
        if srt and Path(srt).is_file():
            return Path(srt)
    return None


def _from_siblings(media: Path) -> Path | None:
    from app.translate.common import srt_stem_and_lang

    found = []
    try:
        entries = list(media.parent.iterdir())
    except OSError:
        return None
    for p in entries:
        if p.suffix.lower() != ".srt" or not p.name.startswith(media.stem + "."):
            continue
        stem, tag = srt_stem_and_lang(p)
        if tag and stem.name == media.stem:
            found.append(p)
    return found[0] if len(found) == 1 else None
