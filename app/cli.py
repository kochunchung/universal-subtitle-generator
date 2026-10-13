"""命令列介面(第 1 階段測試用,GUI 完成後仍可使用)。

  python -m app.cli transcribe 影片1.mp4 [影片2.mkv ...] [選項]
  python -m app.cli calibrate            # 量測顯存,決定最佳批次大小
"""
from __future__ import annotations

from app.paths import setup_env

setup_env()

import argparse  # noqa: E402
import logging  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

from app import settings as settings_mod  # noqa: E402
from app.paths import LOGS_DIR  # noqa: E402

logging.basicConfig(
    filename=LOGS_DIR / "cli.log", level=logging.INFO, encoding="utf-8",
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
for noisy in ("transformers", "huggingface_hub", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.ERROR)


def _quiet_transformers() -> None:
    import warnings

    warnings.filterwarnings("ignore")
    try:
        from transformers.utils import logging as tl

        tl.set_verbosity_error()
    except Exception:
        pass


class Printer:
    """把處理進度印在同一行,避免洗版。"""

    def __init__(self) -> None:
        self.last_stage = None
        self.last_t = 0.0

    def __call__(self, stage: str, p: float, msg: str) -> None:
        now = time.monotonic()
        if p < 0:   # 重要訊息(例如降批次)
            print(f"\n  ! {msg}", flush=True)
            return
        if stage != self.last_stage:
            if self.last_stage is not None:
                print(flush=True)
            self.last_stage = stage
        elif p < 1.0 and now - self.last_t < 0.5:
            return
        self.last_t = now
        print(f"\r  [{stage}] {msg}" + " " * 10, end="", flush=True)


def cmd_transcribe(args) -> int:
    _quiet_transformers()
    from app.core.pipeline import process_file

    s = settings_mod.load()
    if args.model:
        s["whisper_model"] = args.model
    if args.language:
        s["source_language"] = args.language
    if args.batch_size:
        s["batch_size"] = args.batch_size
    if args.no_vad:
        s["vad"] = False
    if args.no_opencc:
        s["opencc_s2twp"] = False
    if args.txt:
        s["write_txt"] = True
    if args.overwrite:
        s["overwrite"] = True
    if args.debug:
        s["debug_words"] = True

    if args.translate_to:
        s["translate_to"] = args.translate_to
    if args.bilingual:
        s["subtitle_mode"] = "bilingual"
    if args.engine:
        s["translation_engine"] = args.engine

    all_files = [Path(f) for f in args.files]
    srt_inputs = [f for f in all_files if f.suffix.lower() == ".srt"]   # 拖入的字幕檔:直接翻譯
    files = [f for f in all_files if f.suffix.lower() != ".srt"]
    if srt_inputs and not s.get("translate_to"):
        print("拖入的 .srt 檔需要指定翻譯目標語言(--translate-to),已略過這些檔案。", flush=True)
        srt_inputs = []
    produced: list[Path] = []
    transcriber = None
    failed = 0
    for i, f in enumerate(files, 1):
        print(f"\n({i}/{len(files)}) {f.name}", flush=True)
        if not f.is_file():
            print("  找不到檔案", flush=True)
            failed += 1
            continue
        rep, transcriber = process_file(
            f, s, transcriber, on_event=Printer(), style_prompt=not args.no_style_prompt,
        )
        print(flush=True)
        print(f"  結果:{rep.message}", flush=True)
        if rep.status == "done":
            print(f"  語言 {rep.language},影片 {rep.duration_s:.0f} 秒,人聲 {rep.speech_s:.0f} 秒,"
                  f"{rep.chunks} 個區塊 → {rep.cues} 條字幕", flush=True)
            print(f"  批次 {rep.batch_size}({rep.batch_note}),顯存峰值 {rep.peak_vram_mb} MB", flush=True)
            t = rep.timings
            print("  耗時:" + "、".join(f"{k} {v:.1f} 秒" for k, v in t.items())
                  + (f"(轉錄速度 {rep.speed} 倍即時)" if rep.speed else ""), flush=True)
            for n in rep.notes[:10]:
                print(f"  註:{n}", flush=True)
            print(f"  字幕:{rep.outputs.get('srt')}", flush=True)
            produced.append(Path(rep.outputs["srt"]))
        elif rep.status == "skipped" and rep.outputs.get("srt"):
            produced.append(Path(rep.outputs["srt"]))
        elif rep.status == "failed":
            failed += 1
    if transcriber is not None:
        transcriber.unload()   # 先釋放 Whisper 的顯存,再載入翻譯模型
    if files:
        print(f"\n轉錄完成,{len(files) - failed}/{len(files)} 個檔案成功。處理報告在 logs\\reports\\", flush=True)

    targets = produced + srt_inputs
    if s.get("translate_to") and targets:
        failed += _run_translation(targets, s["translate_to"], s)
    return 1 if failed else 0


def _run_translation(paths: list[Path], tgt: str, s: dict) -> int:
    """依序翻譯多個 SRT,共用同一個翻譯引擎。回傳失敗數。"""
    from app.translate import common
    from app.translate.runner import make_translator, translate_srt

    engine = s.get("translation_engine", "m2m100")
    mode = "雙語" if s.get("subtitle_mode") == "bilingual" else "只有譯文"
    print(f"\n=== 翻譯成{common.LANG_NAMES.get(tgt, tgt)}({engine},{mode})", flush=True)
    try:
        if engine in ("m2m100", "nllb"):
            from app.settings import OFFLINE_MODELS
            from app.tools.download_model import is_downloaded

            repo = OFFLINE_MODELS[engine]
            if not is_downloaded(repo):
                print(f"  第一次使用,下載翻譯模型 {repo}(約 5 GB)…", flush=True)
                from app.tools.download_model import main as dl_main

                if dl_main([repo]) != 0:
                    print("  模型下載失敗,無法翻譯", flush=True)
                    return len(paths)
            print(f"  載入翻譯模型 {repo} …", flush=True)
        translator = make_translator(engine, s)
    except Exception as e:  # noqa: BLE001
        print(f"  無法啟動翻譯引擎:{e}", flush=True)
        return len(paths)
    failed = 0
    for i, pth in enumerate(paths, 1):
        print(f"\n({i}/{len(paths)}) {pth.name}", flush=True)
        rep = translate_srt(pth, tgt, translator, s, on_event=Printer())
        print(flush=True)
        print(f"  結果:{rep.message}(耗時 {rep.seconds:.1f} 秒)", flush=True)
        for n in rep.notes[:10]:
            print(f"  註:{n}", flush=True)
        failed += rep.status == "failed"
    if hasattr(translator, "unload"):
        translator.unload()
    print(f"\n翻譯完成,{len(paths) - failed}/{len(paths)} 個檔案成功。", flush=True)
    return failed


def cmd_translate(args) -> int:
    _quiet_transformers()
    s = settings_mod.load()
    s["subtitle_mode"] = "bilingual" if args.bilingual else "translated"
    if args.engine:
        s["translation_engine"] = args.engine
    if args.overwrite:
        s["overwrite"] = True
    paths = [Path(f) for f in args.files]
    missing = [p for p in paths if not p.is_file()]
    for p in missing:
        print(f"找不到 {p}", flush=True)
    return _run_translation([p for p in paths if p.is_file()], args.to, s) + len(missing)


def cmd_set_key(args) -> int:
    import getpass

    from app.translate import keys

    s = settings_mod.load()
    provider = args.provider
    print(f"設定 {provider} API 金鑰(輸入時畫面不會顯示,直接貼上後按 Enter;留空則不變更)", flush=True)
    key = getpass.getpass("金鑰:").strip()
    if key:
        keys.set_key(provider, key)
        print("已存到 Windows 認證管理員。", flush=True)
    api = s.setdefault("api", {})
    if provider == "openai":
        cur = api.get("openai_base_url", "")
        url = input(f"服務網址(官方 OpenAI 請留空;目前:{cur or '官方'}):").strip()
        if url:
            api["openai_base_url"] = url
        cur_m = api.get("openai_model", "")
        model = input(f"模型名稱(目前:{cur_m or '未設定'}):").strip()
        if model:
            api["openai_model"] = model
    else:
        cur_m = api.get("anthropic_model", "")
        model = input(f"Claude 模型名稱(留空 = 自動選最新的 Sonnet;目前:{cur_m or '自動'}):").strip()
        if model:
            api["anthropic_model"] = model
    settings_mod.save(s)
    if not keys.has_key(provider):
        print("注意:目前沒有儲存任何金鑰。", flush=True)
        return 1
    print("設定完成。", flush=True)
    return 0


def cmd_calibrate(args) -> int:
    _quiet_transformers()
    from app.core.transcriber import Transcriber
    from app.gpu.budget import calibrate, free_mb, suggest_batch

    s = settings_mod.load()
    model_id = args.model or s["whisper_model"]
    print(f"載入 {model_id} …", flush=True)
    t = Transcriber(model_id)
    print(f"載入完成,剩餘顯存 {free_mb():.0f} MB。開始量測(約 1 分鐘)…", flush=True)
    result = calibrate(t)
    s.setdefault("calibration", {})[model_id] = result
    settings_mod.save(s)
    bs, note = suggest_batch(model_id, s)
    print(f"每筆批次約需 {result['per_item_mb']:.0f} MB(實測 {result['peaks_mb']})", flush=True)
    print(f"建議批次:{note}", flush=True)
    print("結果已寫入 config\\settings.json", flush=True)
    t.unload()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.cli", description="萬能字幕產生器 命令列")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("transcribe", help="影片/音檔 → SRT 字幕")
    p.add_argument("files", nargs="+")
    p.add_argument("--model", help="例如 openai/whisper-large-v3-turbo")
    p.add_argument("--language", help="原文語言代碼,例如 zh、en;預設自動偵測")
    p.add_argument("--batch-size", type=int, help="固定批次大小;預設依顯存自動決定")
    p.add_argument("--no-vad", action="store_true", help="不使用 VAD,直接每 28 秒切一段")
    p.add_argument("--no-opencc", action="store_true", help="中文不轉繁體")
    p.add_argument("--no-style-prompt", action="store_true", help="中文不加「繁體+標點」提示詞")
    p.add_argument("--txt", action="store_true", help="另外輸出純文字 TXT")
    p.add_argument("--overwrite", action="store_true", help="覆蓋已存在的字幕檔與 MP3")
    p.add_argument("--debug", action="store_true", help="輸出每個字的時間到 logs\\reports(檢查對齊用)")
    p.add_argument("--translate-to", choices=["zh-TW", "en", "ja"], help="轉錄後翻譯成這個語言")
    p.add_argument("--bilingual", action="store_true", help="翻譯輸出雙語字幕(上原文、下譯文)")
    p.add_argument("--engine", choices=["m2m100", "nllb", "anthropic", "openai"], help="翻譯引擎")
    p.set_defaults(func=cmd_transcribe)

    t = sub.add_parser("translate", help="翻譯 SRT 字幕檔")
    t.add_argument("files", nargs="+")
    t.add_argument("--to", required=True, choices=["zh-TW", "en", "ja"])
    t.add_argument("--bilingual", action="store_true")
    t.add_argument("--engine", choices=["m2m100", "nllb", "anthropic", "openai"])
    t.add_argument("--overwrite", action="store_true")
    t.set_defaults(func=cmd_translate)

    k = sub.add_parser("set-key", help="設定翻譯 API 金鑰")
    k.add_argument("provider", choices=["anthropic", "openai"])
    k.set_defaults(func=cmd_set_key)

    c = sub.add_parser("calibrate", help="量測顯存用量,決定批次大小")
    c.add_argument("--model")
    c.set_defaults(func=cmd_calibrate)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
