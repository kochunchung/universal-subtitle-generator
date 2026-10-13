"""到期自動清除(第 5 階段)。網站每小時清一次,管理頁也可以立即執行。

規則(規劃文件第 15 節,10/8 確認):
  - 音訊(瀏覽器抽的、伺服器抽的、直接上傳的音檔,以及 worker 轉出的 MP3):
    上傳完成起滿「音訊保存天數」就刪。字幕還在時,項目保留(仍可翻譯、預覽、下載)。
  - 字幕:最後一次產生字幕起滿「字幕保存天數」,整筆項目刪除。
  - 上傳了但沒有字幕的項目:照音訊天數整筆刪除。
  - 等待選音軌的原檔:上傳完成後 1 天(24 小時)還沒選,整筆刪除。
  - 沒傳完的上傳:最後一次收到資料起 1 天,刪除(不能再續傳)。
  - 排隊中、處理中、抽音訊中的項目不動,等下一輪。
天數依每個人「當下」的權限(perms.effective),管理者改了下一輪就生效。
日誌只記數量與大小,不記檔名。
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from app.web import perms, storage
from app.web.config import Config
from app.web.db import DB

log = logging.getLogger("web.cleanup")

BUSY_STATES = ("queued", "running", "transcribed", "extracting")
SUB_KEYS = ("srt", "translated", "txt")


def retention(cfg: Config, p: dict) -> dict:
    return {"audio_s": p["keep_audio_days"] * cfg.day_seconds, "srt_s": p["keep_srt_days"] * cfg.day_seconds,
            "hold_s": cfg.day_seconds}


def audio_files(cfg: Config, item: dict) -> list[Path]:
    """這個項目在伺服器上的音訊(或還沒抽音訊的原檔)。字幕項目沒有音訊。"""
    if item["kind"] != "media":
        return []
    out = []
    p = storage.stored_path(cfg, item)
    if p.is_file():
        out.append(p)
    mp3 = storage.output_file(cfg, item, "mp3")
    if mp3 is not None and mp3 not in out:
        out.append(mp3)
    return out


def has_subtitles(cfg: Config, item: dict) -> bool:
    if item["kind"] == "srt":
        return storage.stored_path(cfg, item).is_file()
    return any(storage.output_file(cfg, item, k) for k in SUB_KEYS)


def deadlines(cfg: Config, item: dict, ret: dict) -> dict:
    """{"audio": 音訊刪除時間或 None, "srt": 字幕(整筆)刪除時間或 None}。"""
    if item["state"] == "choose_track":
        return {"audio": item["created_at"] + ret["hold_s"], "srt": None}
    subs = has_subtitles(cfg, item)
    audio = item["created_at"] + ret["audio_s"] if audio_files(cfg, item) or not subs else None
    srt = (item.get("outputs_at") or item["created_at"]) + ret["srt_s"] if subs else None
    return {"audio": audio, "srt": srt}


def _size(paths) -> int:
    n = 0
    for p in paths:
        try:
            n += p.stat().st_size
        except OSError:
            pass
    return n


def _dir_size(d: Path) -> int:
    return _size(p for p in d.rglob("*") if p.is_file()) if d.is_dir() else 0


def sweep(cfg: Config, db: DB, now: float | None = None) -> dict:
    """清一輪。回傳統計(給管理頁與日誌)。"""
    now = time.time() if now is None else now
    stats = {"items_deleted": 0, "audio_deleted": 0, "uploads_deleted": 0, "bytes_freed": 0}
    busy = db.active_item_ids()
    tiers = perms.tier_defaults(db)
    rets: dict[int, dict] = {}

    def ret_of(uid: int) -> dict | None:
        if uid not in rets:
            u = db.get_user(uid)
            rets[uid] = retention(cfg, perms.effective(db, u, tiers)) if u else None
        return rets[uid]

    for it in db.all_items():
        if it["state"] in BUSY_STATES or it["id"] in busy:
            continue
        ret = ret_of(it["user_id"])
        if ret is None:
            continue
        dl = deadlines(cfg, it, ret)
        subs = dl["srt"] is not None
        if (subs and now >= dl["srt"]) or (not subs and dl["audio"] is not None and now >= dl["audio"]):
            stats["bytes_freed"] += _dir_size(storage.item_dir(cfg, it))
            storage.remove_item_files(cfg, it)
            db.delete_item(it["user_id"], it["id"])
            stats["items_deleted"] += 1
        elif subs and dl["audio"] is not None and now >= dl["audio"]:
            files = audio_files(cfg, it)
            stats["bytes_freed"] += _size(files)
            for f in files:
                f.unlink(missing_ok=True)
            outs = {k: v for k, v in (it.get("outputs") or {}).items() if k != "mp3"}
            info = {**(it.get("info") or {}), "audio_expired": True}
            db.update_item(it["id"], outputs=outs, info=info)
            stats["audio_deleted"] += 1

    from app.web import uploads

    for up in db.all_uploads():
        ret = ret_of(up["user_id"])
        part = uploads.upload_part(cfg, up)
        try:
            last = max(up["created_at"], part.stat().st_mtime)
        except OSError:
            last = up["created_at"]
        if ret is None or now - last >= ret["hold_s"]:
            stats["bytes_freed"] += _size([part])
            part.unlink(missing_ok=True)
            db.delete_upload(up["user_id"], up["id"])
            stats["uploads_deleted"] += 1

    if any(v for k, v in stats.items() if k != "bytes_freed"):
        log.info("到期清除:刪除 %d 個檔案、%d 個檔案的音訊、%d 個沒傳完的上傳,釋放 %.1f MB",
                 stats["items_deleted"], stats["audio_deleted"], stats["uploads_deleted"],
                 stats["bytes_freed"] / 2**20)
    return stats


class Cleaner:
    """網站程序裡的背景執行緒:每小時(驗收時依 WEB_DAY_SECONDS 縮短)清一次。"""

    def __init__(self, cfg: Config, db: DB) -> None:
        self.cfg, self.db = cfg, db
        self.lock = threading.Lock()
        self.interval = min(3600.0, max(5.0, cfg.day_seconds / 24))

    def run_once(self) -> dict:
        with self.lock:              # 管理頁按鈕與排程不同時執行
            return sweep(self.cfg, self.db)

    def run_forever(self, stop: threading.Event) -> None:
        stop.wait(5.0)
        while not stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001  清除失敗不能讓網站停
                log.exception("到期清除失敗")
            stop.wait(self.interval)
