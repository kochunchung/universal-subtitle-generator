"""ffprobe / ffmpeg 操作:讀取音軌資訊、轉成 MP3、讀成 16 kHz 音訊陣列。"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from app.paths import tool_path

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
SAMPLE_RATE = 16000

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv", ".flv", ".m4v", ".ts", ".mts", ".m2ts", ".mpg", ".mpeg", ".3gp"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}


class MediaError(RuntimeError):
    pass


@dataclass
class AudioStream:
    index: int            # 第幾條音軌(從 0 開始,對應 ffmpeg 的 0:a:N)
    codec: str
    channels: int
    language: str | None
    title: str | None

    def label(self) -> str:
        parts = [f"第 {self.index + 1} 軌"]
        if self.language and self.language != "und":
            parts.append(self.language)
        if self.title:
            parts.append(self.title)
        return " · ".join(parts)


@dataclass
class MediaInfo:
    path: Path
    duration: float
    audio_streams: list[AudioStream] = field(default_factory=list)


def _exe(name: str) -> str:
    exe = tool_path(name)
    if not exe:
        raise MediaError(f"找不到 {name},請重新執行 install.bat")
    return exe


def probe(path: str | Path) -> MediaInfo:
    path = Path(path)
    cmd = [
        _exe("ffprobe"), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", "-select_streams", "a", str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise MediaError(f"無法讀取檔案資訊:{r.stderr.decode('utf-8', 'replace').strip()[:300]}")
    data = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
    streams = []
    for i, s in enumerate(data.get("streams", [])):
        tags = s.get("tags", {}) or {}
        streams.append(AudioStream(
            index=i,
            codec=s.get("codec_name", "?"),
            channels=int(s.get("channels", 0) or 0),
            language=tags.get("language"),
            title=tags.get("title"),
        ))
    duration = float((data.get("format") or {}).get("duration") or 0.0)
    if not streams:
        raise MediaError("這個檔案沒有音軌")
    return MediaInfo(path=path, duration=duration, audio_streams=streams)


def _run_with_progress(cmd: list[str], duration: float,
                       on_progress: Callable[[float], None] | None) -> None:
    """執行 ffmpeg 並解析 -progress 輸出,回報 0~1 的進度。"""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW,
    )
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").strip()
        if on_progress and duration > 0 and line.startswith("out_time_us="):
            try:
                us = int(line.split("=", 1)[1])
                on_progress(min(1.0, us / 1e6 / duration))
            except ValueError:
                pass
    err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
    if proc.wait() != 0:
        raise MediaError(f"ffmpeg 轉檔失敗:{err.strip()[-400:]}")
    if on_progress:
        on_progress(1.0)


def extract_mp3(src: str | Path, dst: str | Path, stream_index: int = 0, duration: float = 0.0,
                on_progress: Callable[[float], None] | None = None) -> Path:
    """取出指定音軌,轉成單聲道 128 kbps MP3。先寫暫存檔,完成後才改名,避免留下半個檔案。"""
    dst = Path(dst)
    tmp = dst.with_name(dst.stem + ".part.mp3")
    cmd = [
        _exe("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
        "-i", str(src), "-map", f"0:a:{stream_index}", "-vn", "-sn", "-dn",
        "-ac", "1", "-codec:a", "libmp3lame", "-b:a", "128k",
        "-progress", "pipe:1", "-nostats", str(tmp),
    ]
    try:
        _run_with_progress(cmd, duration, on_progress)
        tmp.replace(dst)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return dst


def load_audio(path: str | Path, stream_index: int = 0) -> np.ndarray:
    """讀成 16 kHz 單聲道 float32 陣列(Whisper 與 VAD 的輸入格式)。"""
    cmd = [
        _exe("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", str(path), "-map", f"0:a:{stream_index}", "-vn",
        "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-",
    ]
    r = subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise MediaError(f"讀取音訊失敗:{r.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    audio = np.frombuffer(r.stdout, dtype=np.float32).copy()
    if audio.size == 0:
        raise MediaError("音訊是空的")
    return audio
