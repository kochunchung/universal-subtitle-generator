"""環境檢查:確認 Python、PyTorch/CUDA、顯存監控、ffmpeg、套件與模型都正常。

命令列:python -m app.envcheck [--smoke]
  --smoke  另外在 GPU 上載入 turbo 模型,轉錄 5 秒測試音訊,並記錄速度與顯存峰值
GUI 會直接呼叫 run_checks()。
"""
from __future__ import annotations

from app.paths import setup_env

setup_env()

import argparse  # noqa: E402
import importlib  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from datetime import datetime  # noqa: E402
from typing import Callable  # noqa: E402

from app.paths import LOGS_DIR, ROOT, tool_path  # noqa: E402
from app.settings import DEFAULT_WHISPER, resolve_model  # noqa: E402

OK, WARN, FAIL = "ok", "warn", "fail"
_MARK = {OK: "[OK]", WARN: "[!!]", FAIL: "[X] "}

# (匯入名稱, 顯示名稱)
PACKAGES = [
    ("transformers", "transformers"),
    ("accelerate", "accelerate"),
    ("PySide6", "PySide6"),
    ("silero_vad", "silero-vad"),
    ("opencc", "opencc"),
    ("rjieba", "rjieba"),
    ("sentencepiece", "sentencepiece"),
    ("huggingface_hub", "huggingface_hub"),
    ("keyring", "keyring"),
    ("anthropic", "anthropic"),
    ("openai", "openai"),
]

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # 不跳出黑色主控台視窗


@dataclass
class CheckResult:
    name: str
    level: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.level != FAIL


def _check_python() -> CheckResult:
    v = sys.version_info
    text = f"{v.major}.{v.minor}.{v.micro}({sys.executable})"
    if (v.major, v.minor) == (3, 11):
        return CheckResult("Python", OK, text)
    return CheckResult("Python", WARN, text + ",預期為 3.11")


def _check_torch() -> list[CheckResult]:
    try:
        import torch
    except Exception as e:
        return [CheckResult("PyTorch", FAIL, f"無法載入:{e}")]
    out = [CheckResult("PyTorch", OK, f"{torch.__version__},CUDA 版本 {torch.version.cuda}")]
    if torch.version.cuda is None:
        out[0] = CheckResult("PyTorch", FAIL, f"{torch.__version__} 是 CPU 版,請重新執行 install.bat")
        return out
    if not torch.__version__.startswith("2.5.1"):
        out[0] = CheckResult("PyTorch", WARN, out[0].detail + ",預期為 2.5.1(cu121)")
    if not torch.cuda.is_available():
        out.append(CheckResult("CUDA", FAIL, "PyTorch 偵測不到顯示卡,請確認驅動已安裝並重新開機"))
        return out
    props = torch.cuda.get_device_properties(0)
    free, total = torch.cuda.mem_get_info(0)
    out.append(CheckResult(
        "CUDA",
        OK,
        f"{props.name},總顯存 {total / 2**30:.1f} GB,目前可用 {free / 2**30:.1f} GB",
    ))
    if total < 5.5 * 2**30:
        out[-1].level = WARN
        out[-1].detail += "(低於 6 GB,建議使用 medium 模型)"
    return out


def _check_nvml() -> CheckResult:
    from app.gpu.monitor import GpuMonitor

    mon = GpuMonitor()
    st = mon.read()
    if st is None:
        return CheckResult("顯存監控(NVML)", WARN, f"無法讀取,介面將不顯示顯存資訊:{mon.error}")
    temp = f"{st.temperature_c}°C" if st.temperature_c is not None else "未知"
    util = f"{st.utilization_pct}%" if st.utilization_pct is not None else "未知"
    return CheckResult(
        "顯存監控(NVML)",
        OK,
        f"驅動 {st.driver},已用 {st.mem_used_mb / 1024:.1f} / {st.mem_total_mb / 1024:.1f} GB,"
        f"溫度 {temp},使用率 {util}",
    )


def _check_tool(name: str) -> CheckResult:
    exe = tool_path(name)
    if not exe:
        return CheckResult(name, FAIL, "找不到,請重新執行 install.bat")
    try:
        r = subprocess.run(
            [exe, "-hide_banner", "-version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, creationflags=NO_WINDOW,
        )
        first = (r.stdout or "").splitlines()[0] if r.stdout else ""
        if r.returncode != 0:
            return CheckResult(name, FAIL, f"執行失敗:{r.stderr.strip()[:200]}")
        return CheckResult(name, OK, first[:90])
    except Exception as e:
        return CheckResult(name, FAIL, f"執行失敗:{e}")


def _check_packages() -> CheckResult:
    from importlib.metadata import PackageNotFoundError, version

    missing, found = [], []
    for mod, dist in PACKAGES:
        try:
            importlib.import_module(mod)
        except Exception as e:
            missing.append(f"{dist}({e.__class__.__name__})")
            continue
        try:
            found.append(f"{dist} {version(dist if dist != 'opencc' else 'opencc-python-reimplemented')}")
        except PackageNotFoundError:
            found.append(dist)
    if missing:
        return CheckResult("Python 套件", FAIL, "缺少或無法載入:" + "、".join(missing))
    return CheckResult("Python 套件", OK, f"{len(found)} 個套件正常")


def _check_model(repo_id: str) -> CheckResult:
    from app.tools.download_model import missing_files

    missing = missing_files(repo_id)
    if not missing:
        where = resolve_model(repo_id)
        src = "手動下載資料夾" if where != repo_id else "Hugging Face 快取"
        return CheckResult("轉錄模型", OK, f"{repo_id} 已就緒({src})")
    return CheckResult("轉錄模型", FAIL, f"{repo_id} 缺少檔案:{'、'.join(missing)}")


def _make_test_audio(seconds: float = 5.0):
    """用 ffmpeg 產生 16 kHz 單聲道測試音(一段和弦音),回傳 float32 numpy 陣列。"""
    import numpy as np

    exe = tool_path("ffmpeg")
    cmd = [
        exe, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}",
        "-filter_complex", "amix=inputs=2",
        "-ac", "1", "-ar", "16000", "-f", "f32le", "-",
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=60, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[:300])
    return np.frombuffer(r.stdout, dtype=np.float32).copy()


def smoke_test(repo_id: str = DEFAULT_WHISPER) -> CheckResult:
    """在 GPU 上實際跑一次轉錄,確認 fp16 + SDPA 可用,並記錄顯存峰值。"""
    name = "GPU 推論測試"
    try:
        import torch
        from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline

        if not torch.cuda.is_available():
            return CheckResult(name, FAIL, "沒有可用的 CUDA 顯示卡")
        audio = _make_test_audio()
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats(0)

        t0 = time.perf_counter()
        source = resolve_model(repo_id)
        model = AutoModelForSpeechSeq2Seq.from_pretrained(
            source,
            torch_dtype=torch.float16,
            low_cpu_mem_usage=True,
            use_safetensors=True,
            attn_implementation="sdpa",
            local_files_only=True,
        ).to("cuda:0")
        processor = AutoProcessor.from_pretrained(source, local_files_only=True)
        asr = pipeline(
            "automatic-speech-recognition",
            model=model,
            tokenizer=processor.tokenizer,
            feature_extractor=processor.feature_extractor,
            torch_dtype=torch.float16,
            device="cuda:0",
        )
        load_s = time.perf_counter() - t0

        t1 = time.perf_counter()
        asr(
            {"raw": audio, "sampling_rate": 16000},
            batch_size=1,
            return_timestamps=True,
            generate_kwargs={"task": "transcribe"},
        )
        torch.cuda.synchronize()
        infer_s = time.perf_counter() - t1
        peak_gb = torch.cuda.max_memory_allocated(0) / 2**30

        del asr, model, processor
        import gc

        gc.collect()
        torch.cuda.empty_cache()
        return CheckResult(
            name, OK,
            f"載入 {load_s:.1f} 秒,推論 {infer_s:.2f} 秒,顯存峰值 {peak_gb:.2f} GB(fp16 + SDPA)",
        )
    except Exception as e:
        return CheckResult(name, FAIL, f"{e.__class__.__name__}:{str(e)[:300]}")


def run_checks(smoke: bool = False,
               on_result: Callable[[CheckResult], None] | None = None) -> list[CheckResult]:
    results: list[CheckResult] = []

    def add(r: CheckResult) -> None:
        results.append(r)
        if on_result:
            on_result(r)

    add(_check_python())
    for r in _check_torch():
        add(r)
    add(_check_nvml())
    add(_check_tool("ffmpeg"))
    add(_check_tool("ffprobe"))
    add(_check_packages())
    add(_check_model(DEFAULT_WHISPER))
    if smoke:
        if all(r.ok for r in results):
            add(smoke_test(DEFAULT_WHISPER))
        else:
            add(CheckResult("GPU 推論測試", WARN, "前面的檢查有失敗項目,略過"))
    return results


def _write_log(results: list[CheckResult]) -> None:
    lines = [f"===== 環境檢查 {datetime.now():%Y-%m-%d %H:%M:%S}({ROOT})"]
    lines += [f"{_MARK[r.level]} {r.name}:{r.detail}" for r in results]
    with open(LOGS_DIR / "envcheck.log", "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="萬能字幕產生器 環境檢查")
    ap.add_argument("--smoke", action="store_true", help="另外執行 GPU 推論測試")
    args = ap.parse_args(argv)

    print("萬能字幕產生器 環境檢查", flush=True)
    results = run_checks(
        smoke=args.smoke,
        on_result=lambda r: print(f"  {_MARK[r.level]} {r.name}:{r.detail}", flush=True),
    )
    _write_log(results)
    failed = [r for r in results if r.level == FAIL]
    if failed:
        print(f"\n有 {len(failed)} 項檢查失敗,詳細紀錄在 logs\\envcheck.log", flush=True)
        return 1
    print("\n全部檢查通過。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
