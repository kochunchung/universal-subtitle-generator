"""翻譯流程:讀 SRT → 翻譯 → 寫出譯文 SRT 或雙語 SRT。輸入與輸出都是 SRT 檔。"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from app.core import srt_io
from app.core.segmenter import Cue
from app.paths import LOGS_DIR
from app.translate import common
from app.translate import glossary as glossary_mod

log = logging.getLogger(__name__)
Event = Callable[[str, float, str], None]


@dataclass
class TranslateReport:
    source: str
    status: str = "pending"      # done / skipped / failed
    message: str = ""
    engine: str = ""
    src_lang: str | None = None
    tgt_lang: str | None = None
    cues: int = 0
    seconds: float = 0.0
    output: str | None = None
    notes: list = field(default_factory=list)


def make_translator(engine: str, settings: dict):
    """建立翻譯引擎。離線引擎會載入模型到 GPU(請先卸載 Whisper)。"""
    if engine in ("m2m100", "nllb"):
        from app.translate.offline import OfflineTranslator

        return OfflineTranslator(engine)
    from app.translate.api import ApiTranslator

    return ApiTranslator(engine, settings)


def glossary_pairs() -> list[tuple[str, str]]:
    """詞彙表「原詞=譯詞」雙向都可用:中翻英用左→右,英翻中用右→左。"""
    g = glossary_mod.load()
    pairs = list(g.translations.items())
    return pairs + [(d, s) for s, d in pairs]


def translate_cues(cues: list[Cue], src: str, tgt: str, translator, on_event: Event,
                   merge: bool = False) -> list[str | None]:
    """回傳與 cues 等長的譯文;翻譯失敗的條目為 None。"""
    pairs = [(s, d) for s, d in glossary_pairs()
             if common.base_lang(common.detect_text_lang(d)) == common.base_lang(tgt)]
    lines = [common.cue_text(c) for c in cues]
    if translator.kind == "api":
        return translator.translate_lines(
            lines, src, tgt, pairs, lambda i, n: on_event("翻譯", i / n, f"翻譯 {i}/{n} 條"))

    to_tw = common.base_lang(tgt) == "zh" and tgt != "zh"
    if to_tw:
        from app.core.postprocess import to_traditional

    if not merge:
        # 逐條翻譯(預設)。實測 NLLB 遇到合併後的長句常把前半句漏掉,逐條反而完整
        res = translator.translate_with_glossary(
            lines, src, tgt, pairs, lambda i, n: on_event("翻譯", i / n, f"翻譯 {i}/{n} 條"))
        return [to_traditional(r) if to_tw else r for r in res]

    # 合併跨條句子後翻譯,再依原文長度比例拆回(設定 offline_merge = true 時使用)
    groups = common.merge_sentences(cues)
    out_g = translator.translate_with_glossary(
        [g.text for g in groups], src, tgt, pairs, lambda i, n: on_event("翻譯", i / n, f"翻譯 {i}/{n} 句"))
    result: list[str | None] = [None] * len(cues)
    for g, tr in zip(groups, out_g):
        if to_tw:
            tr = to_traditional(tr)
        weights = [max(1, len(lines[i])) for i in g.indices]
        for i, part in zip(g.indices, common.split_by_ratio(tr, weights, tgt)):
            result[i] = part
    return result


def build_output(cues: list[Cue], translations: list[str | None], tgt: str, bilingual: bool,
                 line_max: int | None = None) -> list[Cue]:
    """翻譯失敗(None)的條目:雙語模式只留原文;只有譯文模式保留原文,避免字幕整條消失。"""
    out = []
    cjk = common.base_lang(tgt) in common.CJK
    for c, tr in zip(cues, translations):
        if tr and cjk:
            tr = re.sub(r"[。，、,.]+$", "", tr.strip())   # 中日文字幕慣例:句尾不放句號、逗號(保留？！)
        tr_lines = common.wrap_text(tr, tgt, line_max) if tr else []
        if bilingual:
            out.append(Cue(c.start, c.end, [*c.lines, *tr_lines]))
        elif tr_lines:
            out.append(Cue(c.start, c.end, tr_lines))
        elif tr is None:
            out.append(Cue(c.start, c.end, list(c.lines)))
    return out


def planned_output(path: str | Path, tgt: str, bilingual: bool, src_lang: str | None = None,
                   cues: list[Cue] | None = None) -> tuple[Path | None, str]:
    """翻譯這個字幕會產生哪個檔案。回傳(輸出路徑, 原文語言);原文已是目標語言時路徑為 None。"""
    stem, tagged = common.srt_stem_and_lang(path)
    src = src_lang or tagged
    if not src:
        cues = cues if cues is not None else srt_io.read_srt(path)
        src = common.detect_text_lang(" ".join(common.cue_text(c) for c in cues[:50]))
    if common.same_language(src, tgt):
        return None, src
    return common.output_path(stem, src, tgt, bilingual), src


def translate_srt(path: str | Path, tgt: str, translator, settings: dict,
                  on_event: Event = lambda s, p, m: None, src_lang: str | None = None) -> TranslateReport:
    path = Path(path)
    rep = TranslateReport(source=str(path), engine=getattr(translator, "engine", None)
                          or getattr(translator, "provider", ""), tgt_lang=tgt)
    t0 = time.perf_counter()
    try:
        cues = srt_io.read_srt(path)
        if not cues:
            rep.status, rep.message = "failed", "字幕檔是空的或格式無法辨識"
            return rep
        bilingual = settings.get("subtitle_mode") == "bilingual"
        dst, src = planned_output(path, tgt, bilingual, src_lang, cues)
        rep.src_lang, rep.cues = src, len(cues)
        if dst is None:
            rep.status, rep.message = "skipped", f"原文已是{common.LANG_NAMES.get(tgt, tgt)},不需翻譯"
            return rep
        if dst.exists() and not settings.get("overwrite"):
            rep.status, rep.message, rep.output = "skipped", f"已有 {dst.name},略過", str(dst)
            return rep

        on_event("翻譯", 0.0, f"{common.LANG_NAMES.get(src, src)} → {common.LANG_NAMES.get(tgt, tgt)},{len(cues)} 條")
        translations = translate_cues(cues, src, tgt, translator, on_event,
                                      merge=bool(settings.get("offline_merge")))
        failed_n = sum(1 for t in translations if t is None)
        if failed_n == len(cues):
            rep.notes.extend(getattr(translator, "notes", []))
            translator.notes = []
            rep.status, rep.message = "failed", "全部翻譯失敗,沒有產生檔案(請看下方說明)"
            return rep
        seg = settings.get("segmenter") or {}
        line_max = seg.get("max_chars_cjk", 18) if common.base_lang(tgt) in common.CJK else seg.get("max_chars_latin", 42)
        out = build_output(cues, translations, tgt, bilingual, line_max)
        srt_io.write_srt(out, dst)
        rep.output = str(dst)
        rep.notes.extend(getattr(translator, "notes", []))
        translator.notes = []
        rep.status, rep.message = "done", f"完成,輸出 {dst.name}"
        return rep
    except Exception as e:  # noqa: BLE001
        log.exception("翻譯 %s 失敗", path)
        rep.status = "failed"
        rep.message = str(e) if e.__class__.__name__ == "ApiError" else f"{e.__class__.__name__}:{e}"
        return rep
    finally:
        rep.seconds = round(time.perf_counter() - t0, 2)
        d = LOGS_DIR / "reports"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{time.strftime('%Y%m%d-%H%M%S')}_{path.stem}_translate.json").write_text(
            json.dumps(asdict(rep), ensure_ascii=False, indent=2), encoding="utf-8")
