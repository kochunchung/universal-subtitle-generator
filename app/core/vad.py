"""用 Silero VAD 找出有人說話的片段,再組成不超過 30 秒的轉錄區塊。

Whisper 一次只看 30 秒。我們自己切好區塊再整批送進 GPU:
- 只轉錄有人聲的部分,減少靜音與音樂段落的幻覺
- 每個區塊各自轉錄,可以平行批次處理,時間戳也不會在區塊交界錯位
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from app.core.media import SAMPLE_RATE

MAX_CHUNK_S = 28.0      # 留一點餘裕,不要剛好塞滿 30 秒
MERGE_GAP_S = 1.5       # 間隔小於這個值的語音片段,盡量放在同一個區塊


@dataclass
class Chunk:
    start: float   # 秒
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def detect_speech(audio: np.ndarray,
                  on_progress: Callable[[float], None] | None = None) -> list[tuple[float, float]]:
    """回傳有人聲的片段 [(開始秒, 結束秒), ...]。在 CPU 上執行。"""
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad

    model = load_silero_vad(onnx=False)
    wav = torch.from_numpy(audio)
    stamps = get_speech_timestamps(
        wav, model,
        sampling_rate=SAMPLE_RATE,
        threshold=0.45,
        min_speech_duration_ms=250,
        min_silence_duration_ms=400,
        speech_pad_ms=200,
        max_speech_duration_s=MAX_CHUNK_S,   # 超過就在最長靜音處切開
        return_seconds=True,
        progress_tracking_callback=(lambda p: on_progress(p / 100.0)) if on_progress else None,
    )
    return [(float(s["start"]), float(s["end"])) for s in stamps]


def build_chunks(speech: list[tuple[float, float]], total: float) -> list[Chunk]:
    """把語音片段合併成 ≤ MAX_CHUNK_S 秒的區塊。"""
    chunks: list[Chunk] = []
    for s, e in speech:
        s, e = max(0.0, s), min(total, e)
        if e <= s:
            continue
        if chunks and (s - chunks[-1].end) <= MERGE_GAP_S and (e - chunks[-1].start) <= MAX_CHUNK_S:
            chunks[-1].end = e
        else:
            chunks.append(Chunk(s, e))
    # 保險:VAD 已依 max_speech_duration_s 切過,這裡再硬切一次避免意外
    out: list[Chunk] = []
    for c in chunks:
        t = c.start
        while c.end - t > MAX_CHUNK_S + 0.5:
            out.append(Chunk(t, t + MAX_CHUNK_S))
            t += MAX_CHUNK_S
        out.append(Chunk(t, c.end))
    return out


def fixed_chunks(total: float) -> list[Chunk]:
    """不使用 VAD 時,直接每 MAX_CHUNK_S 秒切一塊。"""
    out, t = [], 0.0
    while t < total:
        out.append(Chunk(t, min(total, t + MAX_CHUNK_S)))
        t += MAX_CHUNK_S
    return out


def slice_audio(audio: np.ndarray, chunk: Chunk) -> np.ndarray:
    a = int(chunk.start * SAMPLE_RATE)
    b = int(chunk.end * SAMPLE_RATE)
    return audio[a:b]
