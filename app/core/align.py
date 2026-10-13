"""逐字時間對齊(依 OpenAI Whisper 的 find_alignment 方法)。

做法:轉錄完成後,把「起始符號 + 轉出的文字」再送進解碼器一次,取出指定注意力頭
對音訊的注意力權重,用動態時間校正(DTW)找出每個字在音訊中的位置。

為什麼不用 transformers 內建的逐字時間戳:它會把提示詞(詞彙表、繁體提示)也算進對齊,
導致前段的字全部擠到區塊開頭、整段時間被壓縮。這裡對齊時不放提示詞,所以不受影響。
"""
from __future__ import annotations

import numpy as np

FRAMES_PER_SECOND = 50   # 編碼器每個輸出 = 20 毫秒


def dtw(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """對 cost 矩陣 x(字數 × 音框數)做 DTW,回傳(文字索引, 時間索引)路徑。
    沿反對角線向量化,結果與 transformers 的逐格迴圈版本相同,但快上百倍。"""
    n, m = x.shape
    cost = np.full((n + 1, m + 1), np.inf, dtype=np.float64)
    trace = np.full((n + 1, m + 1), -1, dtype=np.int8)
    cost[0, 0] = 0.0
    for d in range(2, n + m + 1):
        i_lo, i_hi = max(1, d - m), min(n, d - 1)
        if i_lo > i_hi:
            continue
        i = np.arange(i_lo, i_hi + 1)
        j = d - i
        c0 = cost[i - 1, j - 1]
        c1 = cost[i - 1, j]
        c2 = cost[i, j - 1]
        m0 = (c0 < c1) & (c0 < c2)
        m1 = ~m0 & (c1 < c0) & (c1 < c2)
        cost[i, j] = x[i - 1, j - 1] + np.where(m0, c0, np.where(m1, c1, c2))
        t = np.full(i.shape, 2, dtype=np.int8)
        t[m0] = 0
        t[m1] = 1
        trace[i, j] = t

    trace[0, :] = 2
    trace[:, 0] = 1
    i, j = n, m
    text_idx, time_idx = [], []
    while i > 0 or j > 0:
        text_idx.append(i - 1)
        time_idx.append(j - 1)
        t = trace[i, j]
        if t == 0:
            i -= 1
            j -= 1
        elif t == 1:
            i -= 1
        else:
            j -= 1
    return np.array(text_idx[::-1]), np.array(time_idx[::-1])


def median_filter(x: np.ndarray, width: int) -> np.ndarray:
    """沿最後一維做中位數濾波(x 形狀為 [頭數, 字數, 音框數])。"""
    pad = width // 2
    if x.shape[-1] <= pad:
        return x
    padded = np.pad(x, [(0, 0)] * (x.ndim - 1) + [(pad, pad)], mode="reflect")
    windows = np.lib.stride_tricks.sliding_window_view(padded, width, axis=-1)
    return np.median(windows, axis=-1)


def token_times(weights: np.ndarray, n_text_tokens: int, n_frames: int,
                prefix_len: int, filter_width: int = 7) -> np.ndarray:
    """由交叉注意力權重算出每個文字 token 的開始時間(秒)。

    weights:[對齊頭數, 輸入長度, 1500],輸入 = 前綴(prefix_len 個) + 文字 + 結束符號
    回傳長度 n_text_tokens + 1:第 k 個值是第 k 個 token 的開始時間,最後一個是結束時間。
    """
    w = weights[:, :, : max(1, n_frames // 2)].astype(np.float64)
    std = w.std(axis=-2, keepdims=True)
    mean = w.mean(axis=-2, keepdims=True)
    w = (w - mean) / np.where(std == 0, 1.0, std)
    w = median_filter(w, filter_width)
    matrix = w.mean(axis=0)
    # 第 prefix_len-1 列(<|notimestamps|>)負責產生第一個文字 token,依此類推到最後一個文字 token 產生結束符號
    matrix = matrix[prefix_len - 1: prefix_len + n_text_tokens]
    text_idx, time_idx = dtw(-matrix)
    jumps = np.pad(np.diff(text_idx), (1, 0), constant_values=1).astype(bool)
    times = time_idx[jumps] / FRAMES_PER_SECOND
    if len(times) < n_text_tokens + 1:   # 理論上不會發生,保險起見補齊
        times = np.pad(times, (0, n_text_tokens + 1 - len(times)), mode="edge")
    return times[: n_text_tokens + 1]
