"""Whisper 轉錄:fp16 + SDPA + 批次推論,逐字時間戳,爆顯存自動降批次。

流程(每一批區塊):
  1. 編碼器跑一次,結果同時給「產生文字」與「時間對齊」使用
  2. generate 產生文字(可帶提示詞:繁體/標點提示、詞彙表)
  3. 不帶提示詞,把文字再送進解碼器一次取注意力權重,用 DTW 算出每個字的時間(見 align.py)

用法:
    t = Transcriber("openai/whisper-large-v3-turbo")
    lang = t.detect_language(audios)
    results = t.transcribe(audios, language=lang, batch_size=8)
"""
from __future__ import annotations

import gc
import logging
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from app.core import align
from app.core.media import SAMPLE_RATE
from app.settings import resolve_model

log = logging.getLogger(__name__)

HOP_LENGTH = 160                       # 特徵每格 10 毫秒
CJK_LANGS = {"zh", "ja", "ko", "yue", "th", "lo", "my"}
_PUNCT_ONLY = re.compile(r"^[\s\W_]+$")
_ASCII_ALNUM_END = re.compile(r"[A-Za-z0-9]$")
_ASCII_ALNUM_START = re.compile(r"^[A-Za-z0-9]")


def fit_decoder(texts: list[list[int]], max_positions: int, reserved: int) -> list[list[int]]:
    """把每個區塊的文字 token 截到解碼器放得下的長度(max_positions - reserved)。
    正常語音一個 30 秒區塊遠低於上限;會超過的幾乎都是重複的幻覺句,截掉尾巴不影響內容。"""
    limit = max(0, max_positions - reserved)
    over = sum(len(t) > limit for t in texts)
    if over:
        log.warning("%d 個區塊的文字超過解碼器長度上限 %d,已截斷(多半是重複的幻覺句)", over, limit)
    return [t[:limit] for t in texts]


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class ChunkText:
    text: str
    words: list[Word] = field(default_factory=list)


def _device_and_dtype(device: str | None, dtype: str | None) -> tuple[str, str]:
    """預設用第一張 NVIDIA 顯示卡 + fp16。環境變數 SUBGEN_DEVICE=cpu 可改用 CPU(測試用,很慢)。"""
    import os

    device = device or os.environ.get("SUBGEN_DEVICE") or "cuda:0"
    dtype = dtype or ("float32" if device == "cpu" else "float16")
    return device, dtype


def _is_oom(e: BaseException) -> bool:
    import torch

    if isinstance(e, torch.cuda.OutOfMemoryError):
        return True
    return isinstance(e, RuntimeError) and "out of memory" in str(e).lower()


class Transcriber:
    def __init__(self, model_id: str, device: str | None = None, dtype: str | None = None) -> None:
        import torch
        from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

        device, dtype = _device_and_dtype(device, dtype)
        self.model_id = model_id
        self.device = device
        self.torch_dtype = getattr(torch, dtype)
        source = resolve_model(model_id)
        t0 = time.perf_counter()
        self.model = AutoModelForSpeechSeq2Seq.from_pretrained(
            source,
            torch_dtype=self.torch_dtype,
            low_cpu_mem_usage=True,
            use_safetensors=True,
            attn_implementation="sdpa",
            local_files_only=True,
        ).to(device)
        self.model.eval()
        self.processor = AutoProcessor.from_pretrained(source, local_files_only=True)
        self.tokenizer = self.processor.tokenizer

        tk = self.tokenizer
        self.eot = tk.convert_tokens_to_ids("<|endoftext|>")
        self.sot = tk.convert_tokens_to_ids("<|startoftranscript|>")
        self.no_ts = tk.convert_tokens_to_ids("<|notimestamps|>")
        self.task_transcribe = tk.convert_tokens_to_ids("<|transcribe|>")
        self.alignment_heads = [tuple(h) for h in (self.model.generation_config.alignment_heads or [])]
        self.filter_width = int(getattr(self.model.config, "median_filter_width", 7) or 7)
        self.final_batch_size: int | None = None
        self.fallback_count = 0   # 因文字過少而改用「不帶提示詞」重轉的區塊數
        self.load_seconds = time.perf_counter() - t0
        log.info("模型 %s 載入完成(%.1f 秒,來源 %s)", model_id, self.load_seconds, source)

    # ------------------------------------------------------------------ 共用
    def _features(self, audios: list[np.ndarray]):
        return self.processor.feature_extractor(
            audios, sampling_rate=SAMPLE_RATE, return_tensors="pt"
        ).input_features.to(self.device, dtype=self.torch_dtype)

    def _lang_id(self, language: str) -> int:
        tid = self.tokenizer.convert_tokens_to_ids(f"<|{language}|>")
        if tid is None or tid == self.tokenizer.unk_token_id:
            raise ValueError(f"不支援的語言代碼:{language}")
        return tid

    # ------------------------------------------------------------------ 語言偵測
    def detect_language(self, audios: list[np.ndarray], max_samples: int = 5) -> str:
        """取前幾個區塊偵測語言,多數決。回傳語言代碼,例如 'zh'、'en'。"""
        import torch

        sample = audios[:max_samples]
        if not sample:
            return "en"
        with torch.inference_mode():
            ids = self.model.detect_language(self._features(sample))
        tokens = self.tokenizer.convert_ids_to_tokens(ids.reshape(-1).tolist())
        langs = [t.strip("<|>") for t in tokens]
        lang = Counter(langs).most_common(1)[0][0]
        log.info("語言偵測:%s → %s", langs, lang)
        return lang

    # ------------------------------------------------------------------ 轉錄
    def transcribe(
        self,
        audios: list[np.ndarray],
        language: str,
        batch_size: int = 8,
        prompt: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        on_batch_change: Callable[[int, str], None] | None = None,
    ) -> list[ChunkText]:
        """依序轉錄每個區塊,回傳與 audios 等長的結果。時間戳相對於各區塊的開頭。"""
        prompt_ids = None
        if prompt:
            prompt_ids = self.processor.get_prompt_ids(prompt, return_tensors="pt").to(self.device)

        results: list[ChunkText] = []
        bs = max(1, int(batch_size))
        i, n = 0, len(audios)
        while i < n:
            group = audios[i:i + bs]
            # 檔案的最後一個區塊一律再檢查一次:實測帶提示詞時,片尾最後一句容易被省略
            last = {n - 1 - i} if i <= n - 1 < i + bs else set()
            try:
                results.extend(self._run_batch(group, language, prompt_ids, recheck=last))
            except Exception as e:  # noqa: BLE001
                if not _is_oom(e):
                    raise
                self.release_cache()
                if bs == 1:
                    raise RuntimeError(
                        "顯存不足:批次已降到 1 仍然失敗,請關閉其他使用顯示卡的程式,或改用 medium 模型"
                    ) from e
                bs = max(1, bs // 2)
                log.warning("顯存不足,批次降為 %d 後重試", bs)
                if on_batch_change:
                    on_batch_change(bs, f"顯存不足,批次降為 {bs} 重試")
                continue
            i += len(group)
            if on_progress:
                on_progress(i, n)
        self.final_batch_size = bs
        return results

    def _run_batch(self, group: list[np.ndarray], language: str, prompt_ids,
                   max_new_tokens: int | None = None, recheck: set[int] | None = None) -> list[ChunkText]:
        import torch

        feats = self._features(group)
        n_frames = [min(3000, math.ceil(len(a) / HOP_LENGTH)) for a in group]
        with torch.inference_mode():
            # 1. 編碼器(關閉注意力輸出,維持 SDPA 加速)
            enc = self.model.get_encoder()(feats, output_attentions=False).last_hidden_state
            # 2. 產生文字
            texts = self._generate(enc, language, prompt_ids, max_new_tokens)
            if prompt_ids is not None:
                sparse = [i for i, (tk, a) in enumerate(zip(texts, group))
                          if i in (recheck or set()) or self._too_sparse(tk, len(a) / SAMPLE_RATE, language)]
                if sparse:
                    retry = self._generate(enc[sparse], language, None, max_new_tokens)
                    for i, tk in zip(sparse, retry):
                        if len(tk) > len(texts[i]):
                            texts[i] = tk
                            self.fallback_count += 1
            # 3. 對齊。解碼器最多只有 448 個位置(提示詞前綴 4 個 + 文字 + 結束符號都要放得下);
            #    重複的幻覺句把長度塞滿時,截掉放不下的尾巴,否則對齊時會發生 CUDA device-side assert
            texts = fit_decoder(texts, getattr(self.model.config, "max_target_positions", 448), reserved=5)
            times = self._align(enc, texts, language, n_frames)
        out = []
        for toks, t in zip(texts, times):
            words = self._group_words(toks, t, language)
            out.append(ChunkText("".join(w.text for w in words).strip(), words))
        return out

    def _text_tokens(self, row: list[int]) -> list[int]:
        """取出 <|startoftranscript|> 之後、<|endoftext|> 之前的文字 token。
        提示詞位於 startoftranscript 之前,時間戳與控制符號的編號都大於 endoftext,一併濾掉。"""
        start = 0
        for k in range(len(row) - 1, -1, -1):
            if row[k] == self.sot:
                start = k + 1
                break
        toks = []
        for t in row[start:]:
            if t == self.eot:
                break
            if t < self.eot:
                toks.append(t)
        return toks

    def _generate(self, enc, language: str, prompt_ids, max_new_tokens: int | None) -> list[list[int]]:
        from transformers.modeling_outputs import BaseModelOutput

        # 使用時間戳模式:Whisper 訓練時多半帶時間戳,不帶時間戳時容易整段跳過不轉(實測漏掉 15 秒)
        kw = {"language": language, "task": "transcribe", "return_timestamps": True}
        if prompt_ids is not None:
            kw["prompt_ids"] = prompt_ids
        if max_new_tokens:
            kw["max_new_tokens"] = max_new_tokens
        seqs = self.model.generate(encoder_outputs=BaseModelOutput(last_hidden_state=enc), **kw)
        return [self._text_tokens(r) for r in seqs.tolist()]

    def _too_sparse(self, toks: list[int], seconds: float, language: str) -> bool:
        """文字量明顯少於語音長度,可能是漏轉。中文正常語速約每秒 3~5 字。"""
        if seconds < 6:
            return False
        text = self.tokenizer.decode(toks)
        if language in CJK_LANGS:
            return len(re.sub(r"\s", "", text)) / seconds < 1.5
        return len(text.split()) / seconds < 0.7

    def _align(self, enc, texts: list[list[int]], language: str, n_frames: list[int]) -> list[np.ndarray]:
        import torch

        prefix = [self.sot, self._lang_id(language), self.task_transcribe, self.no_ts]
        seqs = [prefix + t + [self.eot] for t in texts]
        width = max(len(s) for s in seqs)
        ids = torch.tensor([s + [self.eot] * (width - len(s)) for s in seqs], device=self.device)
        dec = self.model.get_decoder()(
            input_ids=ids, encoder_hidden_states=enc, output_attentions=True, use_cache=False,
        )
        cross = dec.cross_attentions           # 每層 [批次, 頭數, 長度, 1500]
        heads = torch.stack([cross[l][:, h] for l, h in self.alignment_heads], dim=1)  # [批次, 對齊頭, 長度, 1500]
        heads = heads.float().cpu().numpy()
        out = []
        for b, toks in enumerate(texts):
            if not toks:
                out.append(np.zeros(1))
                continue
            w = heads[b, :, : len(seqs[b])]
            out.append(align.token_times(w, len(toks), n_frames[b], len(prefix), self.filter_width))
        return out

    def _group_words(self, toks: list[int], times: np.ndarray, language: str) -> list[Word]:
        """把 token 組成字詞。中日韓每個字(或 token)各自一個;其他語言以空白分詞。"""
        if not toks:
            return []
        cjk = language in CJK_LANGS
        # 先依 UTF-8 完整性組成可顯示的片段(一個中文字可能被拆成數個 byte token)
        pieces: list[tuple[str, int, int]] = []
        cur: list[int] = []
        start = 0
        for k, t in enumerate(toks):
            cur.append(t)
            s = self.tokenizer.decode(cur)
            if "�" not in s:
                pieces.append((s, start, k + 1))
                cur, start = [], k + 1
        if cur:
            pieces.append((self.tokenizer.decode(cur).replace("�", ""), start, len(toks)))

        words: list[list] = []   # [文字, 開始 token 索引, 結束 token 索引]
        for text, a, b in pieces:
            if not text:
                continue
            if words:
                prev = words[-1][0]
                merge = (
                    (_PUNCT_ONLY.match(text) and not text.startswith(" "))           # 標點併入前一個字
                    or (not cjk and not text.startswith(" "))                       # 英文等:同一個單字的後半
                    or (_ASCII_ALNUM_END.search(prev) and _ASCII_ALNUM_START.match(text))  # 中文裡的英文單字
                )
                if merge:
                    words[-1][0] += text
                    words[-1][2] = b
                    continue
            words.append([text, a, b])

        out = []
        for text, a, b in words:
            s, e = float(times[a]), float(times[b])
            out.append(Word(text, s, max(s, e)))
        _limit_long_words(out)
        return out

    # ------------------------------------------------------------------ 釋放
    def release_cache(self) -> None:
        import torch

        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def unload(self) -> None:
        for attr in ("model", "processor", "tokenizer"):
            if hasattr(self, attr):
                delattr(self, attr)
        self.release_cache()


def _limit_long_words(words: list[Word]) -> None:
    """DTW 有時會把停頓算進某個字,讓它異常地長;依中位數字長截短(作法同 OpenAI Whisper)。"""
    durs = [w.end - w.start for w in words if w.end > w.start]
    if not durs:
        return
    max_d = max(0.7, float(np.median(durs)) * 2)
    for w in words:
        if w.end - w.start > max_d:
            if _PUNCT_ONLY.search(w.text[-1:] or ""):
                w.end = w.start + max_d       # 句尾停頓:截掉後面
            else:
                w.start = max(w.start, w.end - max_d)   # 句首停頓:截掉前面
