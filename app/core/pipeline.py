"""單一檔案的完整處理流程:轉 MP3 → VAD → 轉錄 → 後處理 → 斷句 → 寫出 SRT。

GUI(第 3 階段)與命令列共用這裡的 process_file()。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from app.core import media, postprocess, srt_io, vad
from app.core.segmenter import Segmenter, SegmenterConfig
from app.core.transcriber import Transcriber, Word
from app.paths import LOGS_DIR
from app.translate import glossary as glossary_mod

log = logging.getLogger(__name__)

# 中文時附在提示詞前面:讓 Whisper 傾向輸出繁體字與標點符號(斷句需要標點)
ZH_STYLE_PROMPT = "以下是繁體中文的內容，請加上標點符號。"

Event = Callable[[str, float, str], None]   # (階段, 0~1 進度, 訊息)


@dataclass
class FileReport:
    source: str
    status: str = "pending"          # done / skipped / no_speech / failed
    message: str = ""
    language: str | None = None
    duration_s: float = 0.0
    speech_s: float = 0.0
    chunks: int = 0
    cues: int = 0
    batch_size: int | None = None
    batch_note: str = ""
    peak_vram_mb: float | None = None
    timings: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    @property
    def speed(self) -> float | None:
        t = self.timings.get("轉錄")
        return round(self.duration_s / t, 1) if t else None


class _Timer:
    def __init__(self, report: FileReport, name: str) -> None:
        self.report, self.name = report, name

    def __enter__(self):
        self.t0 = time.perf_counter()

    def __exit__(self, *exc):
        self.report.timings[self.name] = round(time.perf_counter() - self.t0, 2)


def _noop(stage: str, p: float, msg: str) -> None:
    pass


def process_file(
    src: str | Path,
    settings: dict,
    transcriber: Transcriber | None = None,
    on_event: Event = _noop,
    audio_stream: int = 0,
    style_prompt: bool = True,
) -> tuple[FileReport, Transcriber | None]:
    """處理一個檔案。傳入已載入的 transcriber 可在批次處理時重複使用模型。
    回傳(報告, transcriber)。"""
    import torch

    src = Path(src)
    rep = FileReport(source=str(src))
    model_id = settings["whisper_model"]
    overwrite = bool(settings.get("overwrite"))
    traditional = bool(settings.get("opencc_s2twp", True))
    t_all = time.perf_counter()
    created_mp3 = False

    try:
        # 1. 讀取檔案資訊 ---------------------------------------------------
        on_event("讀取", 0.0, f"讀取 {src.name}")
        info = media.probe(src)
        rep.duration_s = round(info.duration, 2)
        if audio_stream >= len(info.audio_streams):
            audio_stream = 0

        forced_lang = settings.get("source_language")
        forced_lang = None if forced_lang in (None, "", "auto") else forced_lang
        if forced_lang and not overwrite:
            paths = srt_io.output_paths(src, srt_io.lang_tag(forced_lang, traditional))
            if paths["srt"].exists():
                rep.status, rep.message = "skipped", f"已有字幕檔 {paths['srt'].name},略過"
                rep.outputs["srt"] = str(paths["srt"])
                return rep, transcriber

        # 2. 轉 MP3 -----------------------------------------------------------
        mp3 = srt_io.output_paths(src, "x")["mp3"]
        with _Timer(rep, "轉MP3"):
            if src.suffix.lower() == ".mp3":
                on_event("轉MP3", 1.0, "來源已是 MP3")
            elif mp3.exists() and not overwrite:
                on_event("轉MP3", 1.0, f"使用既有的 {mp3.name}")
            else:
                media.extract_mp3(src, mp3, audio_stream, info.duration,
                                  lambda p: on_event("轉MP3", p, f"轉 MP3 {p:.0%}"))
                created_mp3 = True
        rep.outputs["mp3"] = str(mp3)

        # 3. 讀音訊 + VAD -------------------------------------------------------
        with _Timer(rep, "VAD"):
            on_event("VAD", 0.0, "讀取音訊")
            audio = media.load_audio(mp3)
            total = len(audio) / media.SAMPLE_RATE
            if settings.get("vad", True):
                speech = vad.detect_speech(audio, lambda p: on_event("VAD", p, f"偵測人聲 {p:.0%}"))
                chunks = vad.build_chunks(speech, total)
            else:
                chunks = vad.fixed_chunks(total)
        rep.chunks = len(chunks)
        rep.speech_s = round(sum(c.duration for c in chunks), 1)
        if not chunks:
            rep.status, rep.message = "no_speech", "沒有偵測到人聲"
            return rep, transcriber
        pieces = [vad.slice_audio(audio, c) for c in chunks]

        # 4. 載入模型 ----------------------------------------------------------
        if transcriber is None or transcriber.model_id != model_id:
            if transcriber is not None:
                transcriber.unload()
            on_event("載入模型", 0.0, f"載入 {model_id}")
            with _Timer(rep, "載入模型"):
                transcriber = Transcriber(model_id)

        # 5. 語言 ----------------------------------------------------------------
        lang = forced_lang or transcriber.detect_language(pieces)
        rep.language = lang
        paths = srt_io.output_paths(src, srt_io.lang_tag(lang, traditional))
        if paths["srt"].exists() and not overwrite:
            rep.status, rep.message = "skipped", f"已有字幕檔 {paths['srt'].name},略過"
            rep.outputs["srt"] = str(paths["srt"])
            return rep, transcriber

        # 6. 轉錄 ----------------------------------------------------------------
        from app.gpu.budget import suggest_batch

        bs, note = suggest_batch(model_id, settings)
        rep.batch_size, rep.batch_note = bs, note
        on_event("轉錄", 0.0, f"語言 {lang},{len(chunks)} 個區塊,{note}")

        gl = glossary_mod.load()
        prompt_parts = []
        if style_prompt and lang == "zh":
            prompt_parts.append(ZH_STYLE_PROMPT)
        if gl.terms:
            prompt_parts.append(gl.prompt_text())
        prompt = " ".join(prompt_parts) or None

        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
        with _Timer(rep, "轉錄"):
            results = transcriber.transcribe(
                pieces, language=lang, batch_size=bs, prompt=prompt,
                on_progress=lambda i, n: on_event("轉錄", i / n, f"轉錄 {i}/{n} 區塊"),
                on_batch_change=lambda b, m: (rep.notes.append(m), on_event("轉錄", -1, m)),
            )
        rep.batch_size = transcriber.final_batch_size
        if transcriber.fallback_count:
            rep.notes.append(f"{transcriber.fallback_count} 個區塊改用不帶提示詞的轉錄結果(內容較完整)")
            transcriber.fallback_count = 0
        if torch.cuda.is_available():
            rep.peak_vram_mb = round(torch.cuda.max_memory_allocated() / 2**20, 1)

        # 7. 後處理 + 斷句 -------------------------------------------------------
        with _Timer(rep, "斷句"):
            words: list[Word] = []
            for ch, res in zip(chunks, results):
                cleaned, note = postprocess.clean_chunk(res)
                if note:
                    rep.notes.append(f"{_fmt(ch.start)} {note}")
                for w in cleaned.words:
                    # 時間戳限制在區塊範圍內,再加上區塊在整段音訊中的位置
                    s = min(max(0.0, w.start), ch.duration) + ch.start
                    e = min(max(w.start, w.end), ch.duration) + ch.start
                    words.append(Word(w.text, s, max(s, e)))
            if settings.get("debug_words"):
                _save_words(src, words)
            cues = Segmenter(lang, SegmenterConfig.from_settings(settings.get("segmenter"))).segment(words)
            if traditional and lang == "zh":
                for c in cues:
                    c.lines = [postprocess.to_traditional(l) for l in c.lines]
        rep.cues = len(cues)

        # 8. 輸出 --------------------------------------------------------------
        srt_io.write_srt(cues, paths["srt"])
        rep.outputs["srt"] = str(paths["srt"])
        if settings.get("write_txt"):
            srt_io.write_txt(cues, paths["txt"])
            rep.outputs["txt"] = str(paths["txt"])
        if settings.get("delete_mp3") and created_mp3:
            mp3.unlink(missing_ok=True)
            rep.outputs.pop("mp3", None)

        rep.status = "done"
        rep.message = f"完成,{len(cues)} 條字幕"
        return rep, transcriber
    except Exception as e:  # noqa: BLE001
        log.exception("處理 %s 失敗", src)
        rep.status, rep.message = "failed", f"{e.__class__.__name__}:{e}"
        return rep, transcriber
    finally:
        rep.timings["總計"] = round(time.perf_counter() - t_all, 2)
        _save_report(rep)


def _fmt(t: float) -> str:
    m, s = divmod(int(t), 60)
    return f"[{m:02d}:{s:02d}]"


def _save_words(src: Path, words: list[Word]) -> None:
    """除錯用:輸出每個字的時間,方便檢查時間對齊。"""
    d = LOGS_DIR / "reports"
    d.mkdir(parents=True, exist_ok=True)
    rows = [{"t": w.text, "s": round(w.start, 2), "e": round(w.end, 2)} for w in words]
    (d / f"{Path(src).stem}_words.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=0), encoding="utf-8")


def _save_report(rep: FileReport) -> None:
    d = LOGS_DIR / "reports"
    d.mkdir(parents=True, exist_ok=True)
    name = f"{time.strftime('%Y%m%d-%H%M%S')}_{Path(rep.source).stem}.json"
    data = asdict(rep)
    data["speed_x_realtime"] = rep.speed
    (d / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
