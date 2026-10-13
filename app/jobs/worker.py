"""工作程序:所有用到 GPU 的處理都在這個獨立程序裡執行。

GUI 只負責畫面;按下「取消」時直接結束這個程序,顯存一定會完整釋放。
兩邊透過 multiprocessing.Queue 傳訊息(都是可序列化的 tuple):
  ("log", 文字)
  ("file", 列號, 狀態, 進度0~1或None, 說明)   狀態:running / transcribed / done / skipped / no_speech / failed
  ("outputs", 列號, {"mp3": ..., "srt": ..., "translated": ...})
  ("done",)
每個工作:{"idx", "path", "kind": media/srt, "stream", "overwrite"};
  media = 轉錄(有選翻譯目標時接著翻譯),srt = 只翻譯這個字幕檔(GUI 也用它來「只翻譯」已轉錄的影片)。
處理順序:先把所有影片轉錄完,卸載 Whisper,再一次翻譯全部字幕(只切換一次模型)。
"""
from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path

# 各階段在單一檔案進度條中佔的範圍
STAGE_SPAN = {
    "讀取": (0.00, 0.02), "轉MP3": (0.02, 0.15), "VAD": (0.15, 0.25),
    "載入模型": (0.25, 0.30), "轉錄": (0.30, 0.95), "斷句": (0.95, 1.00),
}


def _redirect_std() -> None:
    """用 pythonw 啟動時沒有主控台,stdout/stderr 是 None,任何 print 都會出錯;改寫到日誌檔。"""
    from app.paths import LOGS_DIR

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    f = open(LOGS_DIR / "worker_console.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = f
    sys.stderr = f   # tqdm 進度條、警告訊息、下載訊息都寫到檔案,不干擾 GUI


def run(jobs: list[dict], settings: dict, q) -> None:
    _redirect_std()
    from app.paths import LOGS_DIR, setup_env

    setup_env()
    logging.basicConfig(
        filename=LOGS_DIR / "worker.log", level=logging.INFO, encoding="utf-8",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("transformers", "huggingface_hub", "urllib3", "httpx", "httpx2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    import warnings

    warnings.filterwarnings("ignore")
    try:
        from transformers.utils import logging as tl

        tl.set_verbosity_error()
    except Exception:
        pass

    try:
        _run(jobs, settings, q)
    except Exception:   # noqa: BLE001
        q.put(("log", "工作程序發生錯誤:\n" + traceback.format_exc()))
        logging.exception("工作程序發生錯誤")
    finally:
        q.put(("done",))


def _ensure_model(repo: str, q, what: str) -> bool:
    from app.tools.download_model import is_downloaded, main as dl_main

    if is_downloaded(repo):
        return True
    q.put(("log", f"第一次使用{what} {repo},開始下載(可能需要數分鐘到數十分鐘)…"))
    ok = dl_main([repo]) == 0
    q.put(("log", "下載完成。" if ok else f"{repo} 下載失敗,詳情見 logs\\worker_console.log"))
    return ok


def _run(jobs: list[dict], settings: dict, q) -> None:
    from app.core.pipeline import process_file

    tgt = settings.get("translate_to")
    translating = bool(tgt)
    scale = 0.8 if translating else 1.0           # 要翻譯時,轉錄只佔進度條前 80%
    media_jobs = [j for j in jobs if j["kind"] == "media"]
    to_translate: list[tuple[int, str, bool]] = []      # (列號, 字幕檔, 是否覆蓋舊譯文)

    # ---------------------------------------------------------------- 轉錄
    if media_jobs and not _ensure_model(settings["whisper_model"], q, "轉錄模型"):
        for j in media_jobs:
            q.put(("file", j["idx"], "failed", None, "轉錄模型無法下載"))
        media_jobs = []

    transcriber = None
    for j in media_jobs:
        idx = j["idx"]
        q.put(("file", idx, "running", 0.0, "開始處理"))

        def ev(stage: str, p: float, msg: str, idx=idx) -> None:
            if p < 0:   # 重要訊息,例如降批次
                q.put(("log", f"[{Path(j['path']).name}] {msg}"))
                return
            lo, hi = STAGE_SPAN.get(stage, (0.0, 1.0))
            q.put(("file", idx, "running", (lo + (hi - lo) * max(0.0, min(1.0, p))) * scale, msg))

        force = bool(j.get("overwrite"))
        js = dict(settings, overwrite=True) if force else settings
        rep, transcriber = process_file(j["path"], js, transcriber, on_event=ev,
                                        audio_stream=int(j.get("stream", 0)))
        q.put(("outputs", idx, dict(rep.outputs)))
        for n in rep.notes:
            q.put(("log", f"[{Path(j['path']).name}] {n}"))
        if rep.status == "done":
            speed = f",{rep.speed} 倍即時" if rep.speed else ""
            peak = f",顯存峰值 {rep.peak_vram_mb:.0f} MB" if rep.peak_vram_mb else ""
            q.put(("log", f"[{Path(j['path']).name}] {rep.message}(語言 {rep.language},批次 {rep.batch_size}"
                          f"{peak}{speed})"))
        if rep.status in ("done", "skipped") and rep.outputs.get("srt") and translating:
            # 重新轉錄過的字幕,舊譯文的時間軸已經對不上,一併覆蓋
            to_translate.append((idx, rep.outputs["srt"], force and rep.status == "done"))
            q.put(("file", idx, "transcribed", scale, rep.message + ",等待翻譯"))
        else:
            q.put(("file", idx, rep.status, 1.0 if rep.status != "failed" else None, rep.message))
    if transcriber is not None:
        transcriber.unload()   # 釋放 Whisper 的顯存,再載入翻譯模型
        q.put(("log", "已卸載轉錄模型,釋放顯存。"))

    # ---------------------------------------------------------------- 翻譯
    for j in jobs:
        if j["kind"] == "srt":
            if translating:
                to_translate.append((j["idx"], j["path"], bool(j.get("overwrite"))))
            else:
                q.put(("file", j["idx"], "skipped", None, "字幕檔需要選擇翻譯目標才會處理"))
    if not to_translate:
        return

    from app.settings import OFFLINE_MODELS
    from app.translate.runner import make_translator, translate_srt

    engine = settings.get("translation_engine", "m2m100")
    try:
        if engine in OFFLINE_MODELS and not _ensure_model(OFFLINE_MODELS[engine], q, "翻譯模型"):
            raise RuntimeError("翻譯模型無法下載")
        q.put(("log", f"啟動翻譯引擎:{engine}"))
        translator = make_translator(engine, settings)
    except Exception as e:   # noqa: BLE001
        for idx, _, _ in to_translate:
            q.put(("file", idx, "failed", None, f"無法啟動翻譯:{e}"))
        return

    transcribed_now = {j["idx"] for j in media_jobs}
    for idx, path, force in to_translate:
        base = scale if idx in transcribed_now else 0.0   # 只翻譯的檔案,進度條從 0 開始
        q.put(("file", idx, "running", base, "翻譯中"))

        def tev(stage: str, p: float, msg: str, idx=idx, base=base) -> None:
            if p >= 0:
                q.put(("file", idx, "running", base + (1 - base) * p, msg))

        ts = dict(settings, overwrite=True) if force else settings
        rep = translate_srt(path, tgt, translator, ts, on_event=tev)
        for n in rep.notes:
            q.put(("log", f"[{Path(path).name}] {n}"))
        if rep.output:
            q.put(("outputs", idx, {"translated": rep.output}))
        q.put(("file", idx, rep.status if rep.status != "skipped" else "done", 1.0 if rep.status != "failed" else None,
               rep.message))
    if hasattr(translator, "unload"):
        translator.unload()
