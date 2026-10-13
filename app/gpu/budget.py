"""顯存預算:依剩餘顯存與實測數據決定批次大小,以及校準每筆批次的顯存用量。"""
from __future__ import annotations

import logging
import time

import numpy as np

log = logging.getLogger(__name__)

RESERVE_MB = 700            # 保留給 Windows 與其他程式突然增加的用量
DEFAULT_PER_ITEM_MB = 400   # 尚未校準時的保守估計
DECODER_ALLOWANCE_MB = 120  # 校準時只產生少量文字,這裡補上長句解碼的額外用量
MAX_BATCH = 16


def free_mb() -> float:
    import torch

    free, _total = torch.cuda.mem_get_info()
    return free / 2**20


def suggest_batch(model_id: str, settings: dict) -> tuple[int, str]:
    """回傳(批次大小, 說明)。模型必須已經載入,剩餘顯存才準確。"""
    import torch

    forced = settings.get("batch_size")
    if forced:
        return int(forced), f"使用設定值 {forced}"
    if not torch.cuda.is_available():
        return 2, "沒有 CUDA 顯示卡,以 CPU 執行(很慢)"
    cal = (settings.get("calibration") or {}).get(model_id) or {}
    per_item = float(cal.get("per_item_mb") or DEFAULT_PER_ITEM_MB)
    avail = free_mb() - RESERVE_MB
    bs = int(max(1, min(MAX_BATCH, avail // per_item)))
    src = "實測" if cal else "預估"
    return bs, f"剩餘顯存 {avail + RESERVE_MB:.0f} MB,每筆約 {per_item:.0f} MB({src})→ 批次 {bs}"


def calibrate(transcriber, sizes: tuple[int, int] = (1, 4)) -> dict:
    """用 30 秒雜訊區塊實際跑兩種批次大小,量出每多一筆要多用多少顯存。"""
    import torch

    rng = np.random.default_rng(0)
    clip = (rng.standard_normal(16000 * 30) * 0.05).astype(np.float32)
    peaks = {}
    for bs in sizes:
        transcriber.release_cache()
        torch.cuda.reset_peak_memory_stats()
        base = torch.cuda.memory_allocated()
        t0 = time.perf_counter()
        transcriber._run_batch([clip.copy() for _ in range(bs)], "zh", None, max_new_tokens=64)
        torch.cuda.synchronize()
        peaks[bs] = (torch.cuda.max_memory_allocated() - base) / 2**20
        log.info("校準 batch=%d:額外顯存 %.0f MB,%.1f 秒", bs, peaks[bs], time.perf_counter() - t0)
    a, b = sizes
    per_item = max(50.0, (peaks[b] - peaks[a]) / (b - a)) + DECODER_ALLOWANCE_MB
    transcriber.release_cache()
    return {
        "per_item_mb": round(per_item, 1),
        "peaks_mb": {str(k): round(v, 1) for k, v in peaks.items()},
        "measured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
