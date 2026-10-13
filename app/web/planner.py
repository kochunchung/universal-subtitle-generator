"""「開始處理」的規劃:決定每個檔案這次要做什麼。

規則照搬桌面版 MainWindow._plan(gui/main_window.py),只是不依賴 GUI:
  - 沒有字幕 → 轉錄(有選翻譯目標時 worker 會接著翻譯)
  - 有字幕而且選了翻譯目標 → 只翻譯(不重新轉錄)
  - 要重做 → 覆蓋
模式 mode:auto(開始處理)/ translate(只翻譯)/ retranslate(重新翻譯)/ retranscribe(重新轉錄)
翻譯輸出的檔名規則直接用 translate.runner.planned_output,不另寫。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.web import storage
from app.web.config import Config

MODES = ("auto", "translate", "retranslate", "retranscribe")
AUDIO_GONE = "音訊已到期刪除,要重新轉錄請重新上傳"


@dataclass
class Plan:
    job: dict | None = None            # 給 worker.run 的工作;None = 這個檔案這次不處理
    reason: str = ""                   # 不處理的原因(給使用者看)
    found_translated: str | None = None  # 發現已經有譯文時,譯文的檔名(要寫回資料庫)


def plan_item(cfg: Config, item: dict, mode: str, s: dict) -> Plan:
    from app.translate.runner import planned_output

    tgt = s.get("translate_to")
    bilingual = s.get("subtitle_mode") == "bilingual"
    media_path = storage.stored_path(cfg, item)
    if item["kind"] == "media":
        if item["state"] == "failed" and item["message"].startswith("無法讀取"):
            return Plan(reason=item["message"])
        audio_gone = not media_path.is_file()        # 音訊已到期清除(第 5 階段)
        if mode == "retranscribe":
            if audio_gone:
                return Plan(reason=AUDIO_GONE)
            return Plan({"kind": "media", "path": str(media_path), "overwrite": True})
        srt = storage.output_file(cfg, item, "srt")
        if srt is None:
            if audio_gone:
                return Plan(reason=AUDIO_GONE)
            if mode == "auto" and item["state"] != "no_speech":
                return Plan({"kind": "media", "path": str(media_path)})
            return Plan(reason="沒有人聲" if item["state"] == "no_speech" else "還沒有轉錄,請先按「開始處理」")
        src_file = srt
    else:
        if mode == "retranscribe":
            return Plan(reason="字幕檔不能轉錄")
        src_file = media_path
    if not tgt:
        return Plan()
    try:
        dst, _src = planned_output(src_file, tgt, bilingual)
    except Exception as e:  # noqa: BLE001
        return Plan(reason=f"無法讀取字幕:{e}")
    if dst is None:
        return Plan(reason=f"原文已是 {tgt},不需翻譯")
    if dst.exists() and mode != "retranslate":
        found = dst.name if Path(dst).parent == Path(src_file).parent else None
        return Plan(reason="" if mode == "auto" else f"已有譯文(要重做請選「重新翻譯」)", found_translated=found)
    return Plan({"kind": "srt", "path": str(src_file), "overwrite": mode == "retranslate"})
