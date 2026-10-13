"""上傳與限制(第 4 階段)。

兩條路到了伺服器都變成同一種輸入(一個音訊檔),再交給現有的 process_file:
  1. 預設:瀏覽器先用 ffmpeg.wasm 抽出單聲道 MP3,只上傳音訊(content="audio")。
  2. 備援:瀏覽器抽不出來時,分塊上傳原檔(content="original"),伺服器用現有的
     media.extract_mp3 抽音訊(和桌面版同一個函式,所以結果相同),抽完立即刪除原檔。
     原檔有多條音軌時,先停在「選擇音軌」,使用者選好才抽。

上限一律在伺服器檢查(網頁上的提示只是方便使用者),數字來自 perms.effective(依會員等級與個別設定):
  單一音訊檔、能不能改傳原檔與單一原檔、單一檔案長度、每人總容量、每人每日轉錄時數、同時排隊的工作數。
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.web import perms, storage
from app.web.config import Config
from app.web.db import DB

log = logging.getLogger("web.uploads")

MAX_SRT_BYTES = 8 * 2**20
CONTENTS = ("audio", "original", "srt")


class UploadError(Exception):
    """可以直接顯示給使用者的錯誤;status 是 HTTP 狀態碼。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


# ---------------------------------------------------------------------- 用量
def fmt_bytes(n: float) -> str:
    return f"{n / 2**30:.1f} GB" if n >= 2**30 else f"{n / 2**20:.0f} MB"


def fmt_minutes(sec: float) -> str:
    m = int(round(sec / 60))
    return f"{m // 60} 小時 {m % 60} 分" if m >= 60 else f"{m} 分鐘"


def user_bytes(cfg: Config, user_id: int) -> int:
    """這個人在伺服器上占用的空間:上傳中的檔案、原檔、音訊、字幕全部算進去。"""
    total = 0
    for root, _dirs, files in os.walk(cfg.user_dir(user_id)):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def day_start(cfg: Config, now: float | None = None) -> float:
    """今天(依設定的時區)午夜的時間戳。"""
    off = cfg.tz_offset_hours * 3600
    now = time.time() if now is None else now
    return (now + off) // 86400 * 86400 - off


def usage(cfg: Config, db: DB, user: dict) -> dict:
    p = perms.effective(db, user)
    return {
        "tier": p["tier"], "tier_label": p["tier_label"], "priority": p["priority"],
        "storage_bytes": user_bytes(cfg, user["id"]), "storage_limit": p["storage_bytes"],
        "today_seconds": db.seconds_since(user["id"], day_start(cfg)), "daily_limit": p["daily_seconds"],
        "limits": {"audio_bytes": p["audio_bytes"], "original_allowed": p["original_allowed"],
                   "original_bytes": p["original_bytes"], "duration_s": p["duration_s"], "srt_bytes": MAX_SRT_BYTES,
                   "queued_jobs": p["queued_jobs"], "chunk_bytes": cfg.chunk_bytes,
                   "keep_audio_days": p["keep_audio_days"], "keep_srt_days": p["keep_srt_days"],
                   "api_translate": p["api_translate"], "offline_translate": p["offline_translate"]},
    }


def check_daily(cfg: Config, db: DB, user: dict, new_seconds: float) -> None:
    if new_seconds <= 0:
        return
    limit = perms.effective(db, user)["daily_seconds"]
    used = db.seconds_since(user["id"], day_start(cfg))
    if used + new_seconds > limit:
        left = max(0.0, limit - used)
        raise UploadError(429, f"今天的處理額度剩 {fmt_minutes(left)},這次需要 {fmt_minutes(new_seconds)}。"
                               f"你每天最多 {fmt_minutes(limit)},明天午夜歸零;只翻譯不算額度。")


# ---------------------------------------------------------------------- 上傳前的檢查
NO_ORIGINAL = "你的帳號不能改傳原檔。請先用桌面版或其他工具轉成 MP3 再上傳,或請管理者開通改傳原檔"


def classify_upload(p: dict, name: str, content: str) -> tuple[str, str, int, str]:
    """回傳(項目類型 media/srt, 伺服器檔名的副檔名, 單一檔案上限, 上限的名稱)。不接受就丟 UploadError。
    p 是 perms.effective 的結果。"""
    kind, ext = storage.classify(name)
    if kind is None:
        raise UploadError(415, "不支援的檔案類型(支援影片、音檔與 .srt 字幕)")
    if kind == "srt":
        return "srt", ".srt", MAX_SRT_BYTES, "字幕檔"
    if content == "audio":                      # 瀏覽器抽好的音訊:原檔名可能是 .mp4,內容是 MP3
        return "media", ".mp3", p["audio_bytes"], "音訊"
    from app.core.media import AUDIO_EXTS

    if ext in AUDIO_EXTS:
        return "media", ext, p["audio_bytes"], "音訊"
    if not p["original_allowed"]:               # 影片原檔:只有開通「改傳原檔」的帳號可以
        raise UploadError(403, NO_ORIGINAL)
    return "media", ext, p["original_bytes"], "原始檔案"


def check_new(cfg: Config, db: DB, user: dict, name: str, size: int, content: str) -> tuple[str, str, int]:
    if content not in CONTENTS:
        raise UploadError(400, "參數錯誤")
    p = perms.effective(db, user)
    kind, ext, limit, what = classify_upload(p, name, content)
    if size <= 0:
        raise UploadError(411, "缺少檔案大小")
    if size > limit:
        raise UploadError(413, f"{what}超過 {fmt_bytes(limit)} 的上限")
    used = user_bytes(cfg, user["id"])
    for u in db.list_uploads(user["id"]):           # 已開始、還沒傳完的上傳,先把剩下的空間預留起來
        used += max(0, u["size"] - upload_received(cfg, u))
    if used + size > p["storage_bytes"]:
        raise UploadError(413, f"空間不足:你已使用 {fmt_bytes(used)},上限 {fmt_bytes(p['storage_bytes'])}。"
                               "請先刪除不需要的檔案。")
    return kind, ext, limit


def upload_part(cfg: Config, up: dict) -> Path:
    return cfg.user_dir(up["user_id"]) / "uploads" / f"{up['id']}.part"


def upload_received(cfg: Config, up: dict) -> int:
    p = upload_part(cfg, up)
    return p.stat().st_size if p.is_file() else 0


# ---------------------------------------------------------------------- 完成上傳
def probe_media(path: Path) -> dict:
    from app.core import media

    try:
        info = media.probe(path)
    except media.MediaError as e:
        log.info("無法讀取上傳的檔案:%s", str(e).replace(str(path), "檔案")[:300])
        if "沒有音軌" in str(e):
            raise UploadError(400, "這個檔案沒有音軌") from None
        raise UploadError(400, "無法讀取這個檔案,可能是格式不支援或檔案已損壞") from None
    return {"duration": round(info.duration, 2), "audio_streams": [s.label() for s in info.audio_streams]}


def finalize(cfg: Config, db: DB, user: dict, part: Path, name: str, content: str,
             extractor: "Extractor | None") -> dict:
    """上傳完成的暫存檔 → 檢查 → 建立項目。原檔交給 extractor 在背景抽音訊。失敗時刪除暫存檔。"""
    p = perms.effective(db, user)
    kind, ext, _limit, _what = classify_upload(p, name, content)
    item_id = storage.new_id()
    d = cfg.item_dir(user["id"], item_id)
    try:
        d.mkdir(parents=True, exist_ok=True)
        stored = storage.stored_name(name, kind, item_id) if content != "audio" else f"{item_id}.mp3"
        dst = d / stored
        shutil.move(str(part), dst)
        if kind == "srt":
            from app.core import srt_io

            if not srt_io.read_srt(dst):
                raise UploadError(400, "字幕檔是空的或格式無法辨識")
            info = {"duration": 0, "audio_streams": []}
        else:
            info = probe_media(dst)
            if info["duration"] > p["duration_s"]:
                raise UploadError(413, f"檔案長度 {fmt_minutes(info['duration'])},超過 {fmt_minutes(p['duration_s'])} 的上限")
        item = db.create_item(user["id"], item_id, name, kind, stored, dst.stat().st_size, info["duration"])
    except BaseException:
        part.unlink(missing_ok=True)
        storage.remove_dir(cfg, d)
        raise
    db.update_item(item_id, info={"audio_streams": info["audio_streams"], "content": content})
    from app.core.media import VIDEO_EXTS

    needs_extract = kind == "media" and content == "original" and ext in VIDEO_EXTS and extractor is not None
    if needs_extract:
        if len(info["audio_streams"]) > 1:
            db.update_item(item_id, state="choose_track", message="這個檔案有多條音軌,請選擇要轉錄的音軌")
        else:
            db.update_item(item_id, state="extracting", progress=0.0, message="伺服器抽音訊中")
            extractor.submit(user["id"], item_id, 0)
    return db.get_item(user["id"], item_id)  # type: ignore[return-value]


# ---------------------------------------------------------------------- 伺服器端抽音訊(備援路線)
class Extractor:
    """在背景執行緒用 media.extract_mp3 抽音訊,抽完刪原檔。同時最多兩個,避免占滿 CPU。"""

    def __init__(self, cfg: Config, db: DB, workers: int = 2) -> None:
        self.cfg, self.db = cfg, db
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="extract")
        self._lock = threading.Lock()

    def submit(self, user_id: int, item_id: str, stream: int):
        return self.pool.submit(self._run, user_id, item_id, stream)

    def recover(self) -> None:
        """伺服器重開後:抽到一半的重新抽;原檔已經不見的標成失敗。"""
        for it in self.db.items_in_state("extracting"):
            if storage.stored_path(self.cfg, it).is_file():
                self.submit(it["user_id"], it["id"], int(it.get("stream") or 0))
            else:
                self.db.update_item(it["id"], state="failed", message="無法讀取:伺服器重新啟動時原檔已不存在,請重新上傳")

    def _run(self, user_id: int, item_id: str, stream: int) -> None:
        from app.core import media

        it = self.db.get_item(user_id, item_id)
        if it is None:
            return
        src = storage.stored_path(self.cfg, it)
        dst = src.with_name(f"{item_id}.mp3")
        last = [0.0]

        def prog(p: float) -> None:
            now = time.monotonic()
            if now - last[0] >= 1.0:
                last[0] = now
                self.db.update_item(item_id, progress=round(p, 3), message=f"伺服器抽音訊中 {p:.0%}")

        try:
            media.extract_mp3(src, dst, stream, it["duration"], prog)
            if self.db.get_item(user_id, item_id) is None:      # 抽的時候被使用者刪掉了
                dst.unlink(missing_ok=True)
                return
            src.unlink(missing_ok=True)                          # 規劃:抽完音訊立即刪除影片
            self.db.update_item(item_id, stored=dst.name, stream=0, size=dst.stat().st_size,
                                state="uploaded", progress=None, message="")
            log.info("項目 %s 抽音訊完成", item_id)
        except Exception as e:  # noqa: BLE001
            log.warning("項目 %s 抽音訊失敗:%s", item_id, e)
            dst.unlink(missing_ok=True)
            if self.db.get_item(user_id, item_id) is not None:
                msg = str(e).replace(str(src), "檔案").replace(str(self.cfg.data_dir), "…")[:300]
                self.db.update_item(item_id, state="failed", message=f"無法讀取:{msg}")

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
