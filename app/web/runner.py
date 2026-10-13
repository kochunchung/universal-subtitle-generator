"""GPU 工作執行器:從資料庫領取工作,交給現有的 worker.run 處理,把進度寫回資料庫。

重點(都沿用桌面版 GUI 的做法):
- 一次只跑一個工作(單張 GPU)。一個網頁工作 = 一次 worker.run:先轉錄全部、卸載 Whisper、再翻譯全部(只切換一次模型)。
- worker 在獨立的 spawn 程序裡執行,用 multiprocessing.Queue 傳訊息:log / file / outputs / done。
- 取消 = 結束整個工作程序(包含它啟動的 ffmpeg),顯存一定完整釋放。
- 這個模組只靠資料庫與資料夾和網站溝通,可以和網站放在同一台機器(當執行緒),也可以獨立執行
  (python -m app.web.runner),之後把 GPU 搬到另一台機器不用改處理程式。
"""
from __future__ import annotations

import importlib
import logging
import multiprocessing as mp
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from app.paths import ROOT
from app.web import config as config_mod
from app.web import apikeys, storage
from app.web.config import Config
from app.web.db import DB

log = logging.getLogger("web.runner")

PROGRESS_MIN_INTERVAL = 1.0    # 同一個檔案的進度事件最短間隔(秒),避免資料庫被進度訊息塞爆
PROGRESS_MIN_DELTA = 0.02
LOG_MAX_CHARS = 2000


# ---------------------------------------------------------------------- 工作程序入口
_KEY_HINTS = (   # 核心的提示寫「執行 set-api-key.bat」(桌面版);網頁版改成指向設定頁,不動核心
    (re.compile(r"請重新執行 set-api-key\.bat 確認金鑰.*?服務網址"), "請到「設定」頁重新填寫金鑰"),
    (re.compile(r"請重新執行 set-api-key\.bat 確認模型名稱與網址"), "請到「設定」頁確認模型名稱"),
    (re.compile(r"請先執行 set-api-key\.bat"), "請先到「設定」頁填寫金鑰"),
    (re.compile(r"執行 set-api-key\.bat"), "到「設定」頁"),
)


def web_hint(text: str) -> str:
    for pat, rep in _KEY_HINTS:
        text = pat.sub(rep, text)
    return text


def _entry(jobs: list[dict], settings: dict, q, target: str | None, env: dict | None = None) -> None:
    """在子程序裡執行。posix 上先建立自己的程序群組,取消時才能連 ffmpeg 一起結束。
    env:只給這個工作的環境變數,例如 APP_GLOSSARY_FILE(這個使用者自己的詞彙表)。"""
    os.environ.update(env or {})
    if os.name != "nt":
        try:
            os.setsid()
        except OSError:
            pass
    if target:      # 測試用:"模組:函式"
        mod, _, fn = target.partition(":")
        run = getattr(importlib.import_module(mod), fn)
    else:
        from app.jobs.worker import run
    run(jobs, settings, q)


def kill_tree(proc) -> None:
    """結束工作程序與它的所有子程序(ffmpeg 等)。"""
    pid = proc.pid
    if pid is None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=0x08000000)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass
    proc.join(10)


# ---------------------------------------------------------------------- 執行器
class Runner:
    def __init__(self, cfg: Config, db: DB) -> None:
        self.cfg, self.db = cfg, db

    def recover(self) -> None:
        for job in self.db.recover_interrupted():
            self._fail_open_items(job, "伺服器重新啟動,工作中斷,請重新送出")
            self.db.add_event(job["id"], job["user_id"], "job", {"status": "failed",
                              "error": "伺服器重新啟動,工作中斷,請重新送出"})
            log.warning("工作 %s 在上次關閉時還在執行,已標成失敗", job["id"])

    def run_forever(self, stop: threading.Event, idle: float = 1.0) -> None:
        self.recover()
        while not stop.is_set():
            try:
                job = self.db.claim_next_job()
                if job is None:
                    stop.wait(idle)
                    continue
                self.run_job(job)
            except Exception:   # noqa: BLE001  執行器本身不能因為一個工作出錯就停止
                log.exception("執行器發生錯誤")
                stop.wait(2.0)

    # ------------------------------------------------------------------ 單一工作
    def run_job(self, job: dict) -> None:
        jid, uid = job["id"], job["user_id"]
        plan = job["plan"]
        items = {e["item_id"]: self.db.get_item(uid, e["item_id"]) for e in plan}
        idx2item = {e["idx"]: items[e["item_id"]] for e in plan if items.get(e["item_id"])}
        # 檔案被刪掉的項目不處理
        work = [{k: e[k] for k in ("idx", "path", "kind", "stream", "overwrite") if k in e}
                for e in plan if e["idx"] in idx2item]
        self.db.add_event(jid, uid, "job", {"status": "running"})
        if not work:
            self.db.finish_job(jid, "failed", "所有檔案都已被刪除")
            self.db.add_event(jid, uid, "job", {"status": "failed", "error": "所有檔案都已被刪除"})
            return

        ctx = mp.get_context("spawn")
        q = ctx.Queue()
        # 每個人用自己的詞彙表;沒有建立過就是空的(不會用到桌面版的 config/glossary.txt)
        env = {"APP_GLOSSARY_FILE": str(self.cfg.glossary_file(uid))}
        # API 翻譯:這時才解密這個人的金鑰,只交給這個工作程序(不寫進資料庫的工作設定、也不寫日誌)
        settings = job["settings"]
        eng = settings.get("translation_engine")
        if settings.get("translate_to") and eng in apikeys.PROVIDERS:
            got = apikeys.for_job(self.cfg, self.db, uid, eng)
            if got is None:
                err = "找不到你的 API 金鑰(可能已刪除),請到「設定」頁重新填寫後再送出"
                self.db.finish_job(jid, "failed", err)
                for it in idx2item.values():
                    self.db.update_item(it["id"], state="failed", message=err)
                self.db.add_event(jid, uid, "job", {"status": "failed", "error": err})
                return
            env[f"APP_API_KEY_{eng.upper()}"] = got[0]
        proc = ctx.Process(target=_entry, args=(work, settings, q, self.cfg.worker_target, env),
                           daemon=True)
        proc.start()
        log.info("工作 %s 開始(%d 個檔案),程序 %s", jid, len(work), proc.pid)
        st = _State(self.cfg, self.db, job, idx2item)
        canceled = False
        last_check = 0.0
        try:
            while True:
                try:
                    msg = q.get(timeout=0.3)
                except queue.Empty:
                    msg = None
                except (EOFError, OSError):      # 工作程序在寫入途中被結束
                    msg = None
                if msg is not None:
                    st.handle(msg)
                    if msg[0] == "done":
                        break
                now = time.monotonic()
                if now - last_check >= 1.0:
                    last_check = now
                    if self.db.cancel_requested(jid):
                        canceled = True
                        kill_tree(proc)
                        break
                if msg is None and not proc.is_alive():
                    break
        finally:
            if proc.is_alive():
                if not canceled and st.got_done:
                    proc.join(15)       # 收到 done 後給它一點時間正常結束
                if proc.is_alive():
                    kill_tree(proc)
            else:
                proc.join(1)
            # 程序結束後,把佇列裡剩下的訊息處理完
            try:
                while True:
                    st.handle(q.get_nowait())
            except (queue.Empty, OSError, ValueError):
                pass
            q.close()
        exitcode = proc.exitcode
        st.flush()

        if canceled:
            self._close_open_items(job, "canceled", "已取消")
            self.db.finish_job(jid, "canceled")
            self.db.add_event(jid, uid, "log", {"text": "已取消,工作程序已結束,顯存已釋放。"})
            self.db.add_event(jid, uid, "job", {"status": "canceled"})
        elif st.got_done and not st.crashed:
            self.db.finish_job(jid, "done")
            self.db.add_event(jid, uid, "job", {"status": "done"})
        elif st.got_done:
            # worker 把例外記成日誌後仍會送出 done:這種情況要算失敗,不能顯示成「完成」
            err = f"處理途中發生錯誤:{st.crash_msg}"
            self._fail_open_items(job, err)
            self.db.finish_job(jid, "failed", err)
            self.db.add_event(jid, uid, "job", {"status": "failed", "error": err})
        else:
            err = f"工作程序意外中止(結束碼 {exitcode})"
            self._fail_open_items(job, err)
            self.db.finish_job(jid, "failed", err)
            self.db.add_event(jid, uid, "log", {"text": err})
            self.db.add_event(jid, uid, "job", {"status": "failed", "error": err})
        log.info("工作 %s 結束,狀態 %s", jid, self.db.get_job_any(jid)["status"])

    # ------------------------------------------------------------------ 收尾
    def _open_items(self, job: dict) -> list[dict]:
        out = []
        for e in job["plan"]:
            it = self.db.get_item(job["user_id"], e["item_id"])
            if it and it["state"] in ("queued", "running", "transcribed"):
                out.append(it)
        return out

    def _close_open_items(self, job: dict, state: str, message: str) -> None:
        for it in self._open_items(job):
            if it["state"] == "transcribed":     # 原文字幕已經完成,只有翻譯沒做
                message = message + "(原文字幕已完成)"
            self.db.update_item(it["id"], state=state, message=message)
            self.db.add_event(job["id"], job["user_id"], "file",
                              {"item_id": it["id"], "state": state, "progress": it["progress"], "message": message})

    def _fail_open_items(self, job: dict, message: str) -> None:
        self._close_open_items(job, "failed", message)


def _last_error(lines: list[str]) -> str:
    """錯誤追蹤裡真正的錯誤訊息(例如 RuntimeError: ...),通常在最後幾行。"""
    for ln in reversed(lines):
        if re.match(r"^\w*(Error|Exception)\b", ln.strip()):
            return ln.strip()
    return lines[-1].strip() if lines else ""


class _State:
    """處理 worker 傳來的訊息:更新項目、寫事件(含節流與名稱替換)。"""

    def __init__(self, cfg: Config, db: DB, job: dict, idx2item: dict[int, dict]) -> None:
        self.cfg, self.db, self.job = cfg, db, job
        self.idx2item = idx2item
        self.got_done = False
        self.crashed = False          # worker 回報「工作程序發生錯誤」
        self.crash_msg = ""
        self.events = 0
        self._last: dict[int, tuple[float, float, str, str]] = {}   # idx -> (時間, 進度, 狀態, 說明)
        self._pending: dict[int, tuple] = {}                        # 被節流略過、還沒寫入的最新進度

    # 使用者看到的文字裡,不能出現伺服器上的項目編號與路徑:換成原檔名
    def _clean(self, text: str) -> str:
        text = str(text)
        if "Traceback (most recent call last)" in text:
            # 完整的錯誤追蹤只寫進伺服器日誌;使用者只看到第一行說明與最後的錯誤訊息
            log.error("工作 %s:%s", self.job["id"], text)
            lines = [ln for ln in text.splitlines() if ln.strip()]
            text = lines[0] + (" " + _last_error(lines) if len(lines) > 1 else "")
        text = text[:LOG_MAX_CHARS]
        for it in self.idx2item.values():
            if it["id"] in text:
                text = text.replace(it["id"], storage.display_stem(it))
        for path in (str(self.cfg.data_dir), str(ROOT)):     # 不讓伺服器路徑出現在畫面上
            text = text.replace(path, "…")
        return web_hint(text)

    def _event(self, kind: str, data: dict, force: bool = False) -> None:
        if not force and self.events >= self.cfg.max_events_per_job:
            return
        self.events += 1
        self.db.add_event(self.job["id"], self.job["user_id"], kind, data)

    def handle(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "log":
            raw = str(msg[1])
            if raw.startswith("工作程序發生錯誤"):
                self.crashed = True
                lines = [ln for ln in raw.splitlines() if ln.strip()]
                self.crash_msg = self._clean(_last_error(lines))[:300]
            self._event("log", {"text": self._clean(raw)}, force=self.crashed)
        elif kind == "file":
            self._file(*msg[1:])
        elif kind == "outputs":
            self._outputs(msg[1], msg[2])
        elif kind == "done":
            self.got_done = True

    def _file(self, idx: int, state: str, progress: float | None, text: str) -> None:
        it = self.idx2item.get(idx)
        if it is None:
            return
        text = self._clean(text or "")
        now = time.monotonic()
        prev = self._last.get(idx)
        quiet = (state == "running" and prev is not None and prev[2] == "running"
                 and now - prev[0] < PROGRESS_MIN_INTERVAL
                 and (progress is None or prev[1] is None or abs(progress - prev[1]) < PROGRESS_MIN_DELTA))
        if quiet:
            self._pending[idx] = (state, progress, text)
            return
        self._pending.pop(idx, None)
        self._write_file(it, idx, state, progress, text, now)

    def _write_file(self, it: dict, idx: int, state: str, progress: float | None, text: str, now: float) -> None:
        fields = {"state": state, "message": text}
        if progress is not None:
            fields["progress"] = float(progress)
        self.db.update_item(it["id"], **fields)
        # 狀態改變(完成、失敗…)的事件一律寫入,不受上限影響
        self._event("file", {"item_id": it["id"], "state": state, "progress": progress, "message": text},
                    force=state != "running")
        self._last[idx] = (now, progress if progress is not None else -1.0, state, text)

    def flush(self) -> None:
        """節流中被略過的最後一筆進度也寫入,讓畫面停在正確的位置。"""
        for idx, (state, progress, text) in list(self._pending.items()):
            it = self.idx2item.get(idx)
            if it:
                self._write_file(it, idx, state, progress, text, time.monotonic())
        self._pending.clear()

    def _outputs(self, idx: int, outs: dict) -> None:
        it = self.idx2item.get(idx)
        if it is None:
            return
        names = {}
        for k, p in (outs or {}).items():
            if k in ("mp3", "srt", "txt", "translated") and p:
                n = storage.to_output_name(self.cfg, it, p)
                if n:
                    names[k] = n
        if names:
            self.db.merge_item_outputs(it["id"], names)
            self._event("outputs", {"item_id": it["id"], "outputs": sorted(names)}, force=True)


def main() -> int:
    """獨立執行(GPU 工作機):python -m app.web.runner"""
    from app.paths import LOGS_DIR, setup_env

    setup_env()
    logging.basicConfig(filename=LOGS_DIR / "web_runner.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config_mod.load()
    runner = Runner(cfg, DB(cfg.db_path, cfg.priority_max_wait_s))
    stop = threading.Event()
    print("GPU 工作執行器已啟動,等待工作…(Ctrl+C 結束)")
    try:
        runner.run_forever(stop)
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
