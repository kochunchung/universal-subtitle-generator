"""SRT / TXT 讀寫與輸出檔名規則。"""
from __future__ import annotations

import re
from pathlib import Path

from app.core.segmenter import Cue

ENCODING = "utf-8-sig"   # 加 BOM,舊款播放器與電視也能正確顯示中文

_TS = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def format_ts(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(cues: list[Cue]) -> str:
    blocks = []
    for i, c in enumerate(cues, 1):
        blocks.append(f"{i}\n{format_ts(c.start)} --> {format_ts(c.end)}\n{c.text}\n")
    return "\n".join(blocks)


def write_srt(cues: list[Cue], path: str | Path) -> Path:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(to_srt(cues), encoding=ENCODING, newline="\r\n")
    tmp.replace(path)
    return path


def write_txt(cues: list[Cue], path: str | Path) -> Path:
    path = Path(path)
    text = "\n".join(c.text.replace("\n", " ") if not _is_cjk_text(c.text) else c.text.replace("\n", "")
                     for c in cues)
    path.write_text(text + "\n", encoding=ENCODING, newline="\r\n")
    return path


def read_srt(path: str | Path) -> list[Cue]:
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp950", "gb18030"):
        try:
            content = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        content = raw.decode("utf-8", "replace")
    cues: list[Cue] = []
    for block in re.split(r"\r?\n\s*\r?\n", content.strip()):
        lines = [l for l in block.splitlines() if l.strip() != ""]
        for j, line in enumerate(lines):
            m = _TS.search(line)
            if m:
                g = [int(x) for x in m.groups()]
                start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
                end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
                cues.append(Cue(start, end, lines[j + 1:]))
                break
    return cues


def _is_cjk_text(text: str) -> bool:
    return any("぀" <= ch <= "ヿ" or "一" <= ch <= "鿿" or "가" <= ch <= "힯" for ch in text)


def lang_tag(language: str | None, traditional: bool) -> str:
    """輸出檔名用的語言代碼:中文轉繁體時用 zh-TW。"""
    lang = (language or "und").lower()
    if lang == "zh":
        return "zh-TW" if traditional else "zh"
    return lang


def output_paths(media: str | Path, lang: str) -> dict[str, Path]:
    media = Path(media)
    stem = media.with_suffix("")
    return {
        "mp3": media if media.suffix.lower() == ".mp3" else stem.with_suffix(".mp3"),
        "srt": Path(f"{stem}.{lang}.srt"),
        "txt": Path(f"{stem}.{lang}.txt"),
    }
