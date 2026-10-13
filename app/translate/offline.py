"""離線翻譯:M2M100(MIT)或 NLLB-200(CC-BY-NC),在本機 GPU 上執行。

這類模型一次只看一句、沒有上下文,所以:
- 先把跨條的句子合併再翻(common.merge_sentences),翻完依長度比例拆回各條
- 詞彙表用「占位符號」:翻譯前把術語換成 ZQX0 這類代號,翻譯後換成指定譯法;
  代號被模型改壞時,改用不套詞彙表的譯文並記錄在日誌(盡力而為)
"""
from __future__ import annotations

import gc
import logging
import re
import time
from typing import Callable

from app.settings import OFFLINE_MODELS, resolve_model
from app.translate.common import base_lang, detect_text_lang

log = logging.getLogger(__name__)

NLLB_CODES = {
    "zh-TW": "zho_Hant", "zh": "zho_Hans", "en": "eng_Latn", "ja": "jpn_Jpan", "ko": "kor_Hang",
    "fr": "fra_Latn", "de": "deu_Latn", "es": "spa_Latn", "vi": "vie_Latn", "th": "tha_Thai", "id": "ind_Latn",
}

Progress = Callable[[int, int], None]


def _is_oom(e: BaseException) -> bool:
    import torch

    return isinstance(e, torch.cuda.OutOfMemoryError) or (
        isinstance(e, RuntimeError) and "out of memory" in str(e).lower())


class OfflineTranslator:
    kind = "offline"

    def __init__(self, engine: str = "m2m100", device: str | None = None, dtype: str | None = None,
                 batch_size: int = 8, num_beams: int = 4) -> None:
        import torch
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

        from app.core.transcriber import _device_and_dtype

        device, dtype = _device_and_dtype(device, dtype)
        self.engine = engine
        self.repo_id = OFFLINE_MODELS[engine]
        self.device = device
        self.batch_size = batch_size
        self.num_beams = num_beams
        self.notes: list[str] = []
        source = resolve_model(self.repo_id)
        t0 = time.perf_counter()
        self.tokenizer = AutoTokenizer.from_pretrained(source, local_files_only=True)
        self.model = AutoModelForSeq2SeqLM.from_pretrained(
            source, torch_dtype=getattr(torch, dtype), low_cpu_mem_usage=True, local_files_only=True,
        ).to(device).eval()
        self.load_seconds = time.perf_counter() - t0
        log.info("翻譯模型 %s 載入完成(%.1f 秒)", self.repo_id, self.load_seconds)

    # ---------------------------------------------------------------- 語言代碼
    def _src_code(self, lang: str) -> str:
        return NLLB_CODES.get(lang, NLLB_CODES.get(base_lang(lang), "eng_Latn")) if self.engine == "nllb" \
            else base_lang(lang)

    def _tgt_bos(self, lang: str) -> int:
        if self.engine == "nllb":
            return self.tokenizer.convert_tokens_to_ids(NLLB_CODES.get(lang, NLLB_CODES.get(base_lang(lang))))
        return self.tokenizer.get_lang_id(base_lang(lang))

    def _prepare_source(self, text: str, src: str) -> str:
        # M2M100 的中文訓練資料以簡體為主,先轉簡體效果較好
        if self.engine == "m2m100" and base_lang(src) == "zh":
            from app.core.postprocess import to_simplified

            return to_simplified(text)
        return text

    # ---------------------------------------------------------------- 翻譯
    def translate(self, texts: list[str], src: str, tgt: str, on_progress: Progress | None = None) -> list[str]:
        """逐句翻譯,保持順序。依長度排序後分批,減少補齊浪費。"""
        import torch

        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        out: list[str] = [""] * len(texts)
        self.tokenizer.src_lang = self._src_code(src)
        bos = self._tgt_bos(tgt)
        bs = self.batch_size
        i, done = 0, 0
        while i < len(order):
            idx = order[i:i + bs]
            batch = [self._prepare_source(texts[k], src) for k in idx]
            try:
                enc = self.tokenizer(batch, return_tensors="pt", padding=True, truncation=True, max_length=256)
                enc = {k: v.to(self.device) for k, v in enc.items()}
                with torch.inference_mode():
                    gen = self.model.generate(
                        **enc, forced_bos_token_id=bos, num_beams=self.num_beams,
                        max_new_tokens=min(400, int(enc["input_ids"].shape[1] * 2.5) + 20),
                    )
                res = self.tokenizer.batch_decode(gen, skip_special_tokens=True)
            except Exception as e:  # noqa: BLE001
                if not _is_oom(e) or bs == 1:
                    raise
                bs = max(1, bs // 2)
                self.notes.append(f"翻譯時顯存不足,批次降為 {bs}")
                self._release()
                continue
            for k, r in zip(idx, res):
                out[k] = r.strip()
            i += len(idx)
            done += len(idx)
            if on_progress:
                on_progress(done, len(texts))
        return out

    def translate_with_glossary(self, texts: list[str], src: str, tgt: str,
                                pairs: list[tuple[str, str]], on_progress: Progress | None = None) -> list[str]:
        """先用占位符號保護術語翻譯;占位符號被改壞的句子,改用一般翻譯。"""
        usable = [(s, d) for s, d in pairs if s and d and _matches_target(d, tgt)]
        if not usable:
            return self.translate(texts, src, tgt, on_progress)
        protected, maps = zip(*(_protect(t, usable) for t in texts))
        first = self.translate(list(protected), src, tgt, on_progress)
        out, retry = list(first), []
        for i, (r, m) in enumerate(zip(first, maps)):
            if not m:
                continue
            restored = _restore(r, m)
            if restored is None:
                retry.append(i)
            else:
                out[i] = restored
        if retry:
            plain = self.translate([texts[i] for i in retry], src, tgt)
            for i, r in zip(retry, plain):
                out[i] = r
            msg = f"{len(retry)} 句的詞彙表占位符號被翻譯模型改動,這些句子改用未套用詞彙表的譯文"
            self.notes.append(msg)
            log.info("%s:%s", msg, [texts[i][:30] for i in retry])
        return out

    def _release(self) -> None:
        import torch

        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def unload(self) -> None:
        for a in ("model", "tokenizer"):
            if hasattr(self, a):
                delattr(self, a)
        self._release()


# ---------------------------------------------------------------- 占位符號
def _matches_target(term: str, tgt: str) -> bool:
    return base_lang(detect_text_lang(term)) == base_lang(tgt)


def _protect(text: str, pairs: list[tuple[str, str]]) -> tuple[str, list[tuple[int, str]]]:
    used: list[tuple[int, str]] = []
    for src, dst in sorted(pairs, key=lambda p: -len(p[0])):
        if src in text:
            n = len(used)
            text = text.replace(src, f" ZQX{n} ")
            used.append((n, dst))
    return re.sub(r"\s{2,}", " ", text).strip(), used


def _restore(text: str, used: list[tuple[int, str]]) -> str | None:
    for n, dst in used:
        rx = re.compile(rf"Z\s*Q\s*X\s*{n}(?!\d)", re.IGNORECASE)
        if not rx.search(text):
            return None
        text = rx.sub(dst, text)
    if re.search(r"ZQX\d", text, re.IGNORECASE):
        return None
    # 中日文譯文:代號兩側多出的空白去掉
    text = re.sub(r"(?<=[　-鿿＀-￯])\s+|\s+(?=[　-鿿＀-￯])", "", text)
    return text.strip()
