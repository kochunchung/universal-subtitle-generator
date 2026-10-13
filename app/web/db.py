"""SQLite 資料庫(WAL 模式)。

資料庫同時是「工作佇列」與「事件紀錄」:網站寫入工作,GPU 工作程序領取工作並寫回進度,
兩邊只透過這裡溝通。之後若 GPU 搬到另一台機器,換成 PostgreSQL 即可,呼叫端不用改。

每個方法自己開關連線(連線很輕),網站的多個執行緒與工作程序可以同時使用。
所有「使用者資料」的查詢都要帶 user_id,這是資料隔離的第一道防線。
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider    TEXT NOT NULL,
    subject     TEXT NOT NULL,
    email       TEXT,
    name        TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending / approved / blocked
    tier        TEXT NOT NULL DEFAULT 'free',      -- free / paid
    is_admin    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    UNIQUE(provider, subject)
);
CREATE TABLE IF NOT EXISTS items (
    id          TEXT PRIMARY KEY,                  -- 隨機字串,不可猜
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,                     -- 使用者看到的原檔名(只用於顯示與下載檔名)
    kind        TEXT NOT NULL,                     -- media / srt
    stored      TEXT NOT NULL,                     -- 伺服器上的安全檔名,例如 media.mp4
    size        INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL DEFAULT 'uploaded',
    progress    REAL,
    message     TEXT NOT NULL DEFAULT '',
    outputs     TEXT NOT NULL DEFAULT '{}',        -- JSON:類型 -> 檔名(相對於這個項目的資料夾)
    stream      INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS items_user ON items(user_id, created_at);
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'queued',    -- queued / running / done / failed / canceled
    mode        TEXT NOT NULL,
    plan        TEXT NOT NULL,                     -- JSON:要交給 worker.run 的工作清單
    settings    TEXT NOT NULL,                     -- JSON:交給 worker.run 的設定(不含任何金鑰)
    cancel      INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS jobs_user ON jobs(user_id, created_at);
CREATE TABLE IF NOT EXISTS events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL,                     -- log / file / outputs / job
    data        TEXT NOT NULL,
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id, seq);
CREATE TABLE IF NOT EXISTS uploads (
    id          TEXT PRIMARY KEY,                  -- 分塊上傳中的檔案(完成後變成 items 的一筆)
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    content     TEXT NOT NULL,                     -- audio(瀏覽器抽好的音訊)/ original(原檔)/ srt
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS uploads_user ON uploads(user_id, created_at);
CREATE TABLE IF NOT EXISTS api_keys (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider    TEXT NOT NULL,                     -- anthropic / openai
    enc         TEXT NOT NULL,                     -- 用 WEB_KEY_SECRET 加密後的金鑰
    last4       TEXT NOT NULL,                     -- 畫面上顯示的末四碼
    model       TEXT NOT NULL DEFAULT '',          -- OpenAI 的模型名稱
    updated_at  REAL NOT NULL,
    PRIMARY KEY (user_id, provider)
);
CREATE TABLE IF NOT EXISTS tiers (
    tier        TEXT PRIMARY KEY,                  -- free / paid:等級的預設權限(管理頁可改)
    data        TEXT NOT NULL,                     -- JSON,項目見 perms.FIELDS
    updated_at  REAL NOT NULL
);
"""

ITEM_JSON = ("outputs", "info")
USER_JSON = ("prefs", "overrides")
JOB_JSON = ("plan", "settings")


def _row(r: sqlite3.Row | None, json_cols: tuple[str, ...] = ()) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for c in json_cols:
        if c in d and isinstance(d[c], str):
            d[c] = json.loads(d[c])
    return d


class DB:
    def __init__(self, path: Path, priority_wait_s: float = 1800) -> None:
        self.path = Path(path)
        # 一般會員的工作等超過這麼久,就和優先的工作一起依送出時間排(避免無限期被插隊)
        self.priority_wait_s = float(priority_wait_s)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            self._migrate(c)
            c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        """舊資料庫補上新欄位(第 2 階段:使用者偏好、檔案長度)。"""
        def cols(table: str) -> set[str]:
            return {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        if "prefs" not in cols("users"):
            c.execute("ALTER TABLE users ADD COLUMN prefs TEXT NOT NULL DEFAULT '{}'")
        if "duration" not in cols("items"):
            c.execute("ALTER TABLE items ADD COLUMN duration REAL NOT NULL DEFAULT 0")
        # 第 4 階段:檔案的音軌資訊、每個工作要轉錄的音訊總秒數(每日額度用)
        if "info" not in cols("items"):
            c.execute("ALTER TABLE items ADD COLUMN info TEXT NOT NULL DEFAULT '{}'")
        if "seconds" not in cols("jobs"):
            c.execute("ALTER TABLE jobs ADD COLUMN seconds REAL NOT NULL DEFAULT 0")
        # 審核機制:最後登入時間,讓管理者判斷帳號
        if "last_login" not in cols("users"):
            c.execute("ALTER TABLE users ADD COLUMN last_login REAL")
        # 第 4.5 階段:個別權限、最後使用時間、工作的排隊優先
        if "overrides" not in cols("users"):
            c.execute("ALTER TABLE users ADD COLUMN overrides TEXT NOT NULL DEFAULT '{}'")
        if "last_seen" not in cols("users"):
            c.execute("ALTER TABLE users ADD COLUMN last_seen REAL")
        if "priority" not in cols("jobs"):
            c.execute("ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
        # 第 5 階段:字幕最後一次產生的時間(字幕保存天數從這時候算)
        if "outputs_at" not in cols("items"):
            c.execute("ALTER TABLE items ADD COLUMN outputs_at REAL")

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)   # 自動提交;需要時自己 BEGIN
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=10000")
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
        finally:
            c.close()

    # ------------------------------------------------------------------ 使用者
    def upsert_user(self, provider: str, subject: str, email: str | None, name: str | None,
                    status: str = "pending", tier: str = "free", is_admin: bool = False) -> dict:
        with self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT * FROM users WHERE provider=? AND subject=?", (provider, subject)).fetchone()
            if r is None:
                c.execute(
                    "INSERT INTO users(provider,subject,email,name,status,tier,is_admin,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (provider, subject, email, name, status, tier, int(is_admin), time.time()))
            else:   # 已存在:只更新顯示用資料,不改核准狀態與等級
                c.execute("UPDATE users SET email=COALESCE(?,email), name=COALESCE(?,name) WHERE id=?",
                          (email, name, r["id"]))
            c.execute("COMMIT")
            return _row(c.execute("SELECT * FROM users WHERE provider=? AND subject=?",
                                  (provider, subject)).fetchone(), USER_JSON)

    def touch_login(self, user_id: int) -> None:
        now = time.time()
        with self.conn() as c:
            c.execute("UPDATE users SET last_login=?, last_seen=? WHERE id=?", (now, now, user_id))

    def touch_seen(self, user_id: int, every: float = 300) -> None:
        """記錄最後使用時間(最多每 5 分鐘寫一次,避免每個請求都寫資料庫)。"""
        now = time.time()
        with self.conn() as c:
            c.execute("UPDATE users SET last_seen=? WHERE id=? AND (last_seen IS NULL OR last_seen<?)",
                      (now, user_id, now - every))

    # ------------------------------------------------------------------ API 金鑰(只存加密後的)
    def set_api_key(self, user_id: int, provider: str, enc: str, last4: str, model: str) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO api_keys(user_id,provider,enc,last4,model,updated_at) VALUES(?,?,?,?,?,?)"
                      " ON CONFLICT(user_id,provider) DO UPDATE SET enc=excluded.enc, last4=excluded.last4,"
                      " model=excluded.model, updated_at=excluded.updated_at",
                      (user_id, provider, enc, last4, model, time.time()))

    def get_api_keys(self, user_id: int) -> dict[str, dict]:
        with self.conn() as c:
            return {r["provider"]: dict(r) for r in
                    c.execute("SELECT * FROM api_keys WHERE user_id=?", (user_id,))}

    def delete_api_key(self, user_id: int, provider: str) -> bool:
        with self.conn() as c:
            return c.execute("DELETE FROM api_keys WHERE user_id=? AND provider=?",
                             (user_id, provider)).rowcount > 0

    # ------------------------------------------------------------------ 等級預設值
    def get_tiers(self) -> dict[str, dict]:
        with self.conn() as c:
            return {r["tier"]: json.loads(r["data"]) for r in c.execute("SELECT tier, data FROM tiers")}

    def set_tier(self, tier: str, data: dict) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO tiers(tier,data,updated_at) VALUES(?,?,?) ON CONFLICT(tier) DO UPDATE"
                      " SET data=excluded.data, updated_at=excluded.updated_at",
                      (tier, json.dumps(data, ensure_ascii=False), time.time()))

    def user_stats(self, user_id: int) -> dict:
        with self.conn() as c:
            items = c.execute("SELECT COUNT(*) FROM items WHERE user_id=?", (user_id,)).fetchone()[0]
            jobs = c.execute("SELECT COUNT(*), COALESCE(SUM(seconds),0) FROM jobs WHERE user_id=?"
                             " AND status IN ('queued','running','done')", (user_id,)).fetchone()
        return {"items": items, "jobs": jobs[0], "seconds_total": float(jobs[1])}

    def get_user(self, user_id: int) -> dict | None:
        with self.conn() as c:
            return _row(c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone(), USER_JSON)

    def list_users(self) -> list[dict]:
        with self.conn() as c:
            return [_row(r, USER_JSON) for r in c.execute("SELECT * FROM users ORDER BY id")]

    def update_user(self, user_id: int, **fields: Any) -> None:
        allowed = {"status", "tier", "is_admin", "name", "email", "prefs", "overrides"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        for k in USER_JSON:
            if k in sets:
                sets[k] = json.dumps(sets[k], ensure_ascii=False)
        if not sets:
            return
        with self.conn() as c:
            c.execute(f"UPDATE users SET {','.join(k + '=?' for k in sets)} WHERE id=?",
                      (*sets.values(), user_id))

    # ------------------------------------------------------------------ 項目(上傳的檔案)
    def create_item(self, user_id: int, item_id: str, name: str, kind: str, stored: str, size: int,
                    duration: float = 0.0) -> dict:
        with self.conn() as c:
            c.execute("INSERT INTO items(id,user_id,name,kind,stored,size,duration,created_at)"
                      " VALUES(?,?,?,?,?,?,?,?)",
                      (item_id, user_id, name, kind, stored, size, float(duration or 0), time.time()))
        return self.get_item(user_id, item_id)  # type: ignore[return-value]

    def get_item(self, user_id: int, item_id: str) -> dict | None:
        with self.conn() as c:
            return _row(c.execute("SELECT * FROM items WHERE id=? AND user_id=?", (item_id, user_id)).fetchone(),
                        ITEM_JSON)

    def get_items(self, user_id: int, item_ids: list[str]) -> list[dict]:
        """依傳入順序回傳這個使用者擁有的項目;不屬於他的編號直接忽略。"""
        out = []
        for i in item_ids:
            it = self.get_item(user_id, i)
            if it:
                out.append(it)
        return out

    def list_items(self, user_id: int) -> list[dict]:
        with self.conn() as c:
            return [_row(r, ITEM_JSON) for r in
                    c.execute("SELECT * FROM items WHERE user_id=? ORDER BY created_at, id", (user_id,))]

    def update_item(self, item_id: str, **fields: Any) -> None:
        allowed = {"state", "progress", "message", "outputs", "stream", "size", "stored", "info", "duration"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        for k in ("outputs", "info"):
            if k in sets:
                sets[k] = json.dumps(sets[k], ensure_ascii=False)
        if not sets:
            return
        with self.conn() as c:
            c.execute(f"UPDATE items SET {','.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), item_id))

    def merge_item_outputs(self, item_id: str, new: dict[str, str]) -> None:
        with self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT outputs FROM items WHERE id=?", (item_id,)).fetchone()
            if r is not None:
                cur = json.loads(r["outputs"])
                cur.update(new)
                c.execute("UPDATE items SET outputs=?, outputs_at=? WHERE id=?",
                          (json.dumps(cur, ensure_ascii=False), time.time(), item_id))
            c.execute("COMMIT")

    def seconds_since(self, user_id: int, since: float) -> float:
        """從 since 之後送出、沒有取消或失敗的工作,總共要轉錄幾秒的音訊(每日額度)。"""
        with self.conn() as c:
            return float(c.execute(
                "SELECT COALESCE(SUM(seconds),0) FROM jobs WHERE user_id=? AND created_at>=?"
                " AND status IN ('queued','running','done')", (user_id, since)).fetchone()[0])

    def all_items(self) -> list[dict]:
        """不分使用者(只給到期清除使用)。"""
        with self.conn() as c:
            return [_row(r, ITEM_JSON) for r in c.execute("SELECT * FROM items ORDER BY user_id")]

    def all_uploads(self) -> list[dict]:
        """不分使用者(只給到期清除使用)。"""
        with self.conn() as c:
            return [_row(r) for r in c.execute("SELECT * FROM uploads")]

    def active_item_ids(self) -> set[str]:
        """排隊中或處理中的工作用到的項目(到期清除時不能動)。"""
        out: set[str] = set()
        with self.conn() as c:
            for r in c.execute("SELECT plan FROM jobs WHERE status IN ('queued','running')"):
                out.update(e.get("item_id") for e in json.loads(r["plan"]))
        return out

    def items_in_state(self, *states: str) -> list[dict]:
        """不分使用者(只給伺服器啟動時的復原使用)。"""
        with self.conn() as c:
            q = ",".join("?" * len(states))
            return [_row(r, ITEM_JSON) for r in c.execute(f"SELECT * FROM items WHERE state IN ({q})", states)]

    # ------------------------------------------------------------------ 分塊上傳
    def create_upload(self, user_id: int, upload_id: str, name: str, size: int, content: str) -> dict:
        with self.conn() as c:
            c.execute("INSERT INTO uploads(id,user_id,name,size,content,created_at) VALUES(?,?,?,?,?,?)",
                      (upload_id, user_id, name, size, content, time.time()))
        return self.get_upload(user_id, upload_id)  # type: ignore[return-value]

    def get_upload(self, user_id: int, upload_id: str) -> dict | None:
        with self.conn() as c:
            return _row(c.execute("SELECT * FROM uploads WHERE id=? AND user_id=?", (upload_id, user_id)).fetchone())

    def list_uploads(self, user_id: int) -> list[dict]:
        with self.conn() as c:
            return [_row(r) for r in c.execute("SELECT * FROM uploads WHERE user_id=? ORDER BY created_at", (user_id,))]

    def delete_upload(self, user_id: int, upload_id: str) -> bool:
        with self.conn() as c:
            return c.execute("DELETE FROM uploads WHERE id=? AND user_id=?", (upload_id, user_id)).rowcount > 0

    def delete_user_data(self, user_id: int) -> None:
        """刪除這個使用者的所有檔案紀錄、工作與事件(帳號本身保留)。"""
        with self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("DELETE FROM events WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM jobs WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM items WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM uploads WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM api_keys WHERE user_id=?", (user_id,))
            c.execute("COMMIT")

    # 排隊順序(付費優先):優先的工作、或已經等超過 priority_wait_s 的工作排前面,同一級先到先服務。
    # 領取工作、排隊位置、預估等待時間都用這同一個排序。
    _QUEUE_ORDER = "ORDER BY (priority=1 OR created_at<=?) DESC, created_at, id"

    def queued_jobs(self, c: sqlite3.Connection | None = None) -> list[dict]:
        """所有排隊中的工作,依輪到的順序。"""
        def run(conn: sqlite3.Connection) -> list[dict]:
            return [_row(x, JOB_JSON) for x in conn.execute(
                "SELECT * FROM jobs WHERE status='queued' " + self._QUEUE_ORDER,
                (time.time() - self.priority_wait_s,))]
        if c is not None:
            return run(c)
        with self.conn() as conn:
            return run(conn)

    def running_jobs(self) -> list[dict]:
        with self.conn() as c:
            return [_row(x, JOB_JSON) for x in c.execute("SELECT * FROM jobs WHERE status='running'")]

    def queued_jobs_before(self, job_id: str) -> list[dict]:
        """排在這個工作前面(含執行中)的工作,用來估計等待時間。"""
        queue = self.queued_jobs()
        ids = [j["id"] for j in queue]
        if job_id not in ids:
            return []
        return self.running_jobs() + queue[:ids.index(job_id)]

    def item_durations(self, item_ids: list[str]) -> float:
        if not item_ids:
            return 0.0
        with self.conn() as c:
            q = ",".join("?" * len(item_ids))
            return float(c.execute(f"SELECT COALESCE(SUM(duration),0) FROM items WHERE id IN ({q})",
                                   item_ids).fetchone()[0])

    def delete_item(self, user_id: int, item_id: str) -> bool:
        with self.conn() as c:
            return c.execute("DELETE FROM items WHERE id=? AND user_id=?", (item_id, user_id)).rowcount > 0

    # ------------------------------------------------------------------ 工作
    def create_job(self, user_id: int, job_id: str, mode: str, plan: list[dict], settings: dict,
                   seconds: float = 0.0, priority: bool = False) -> dict:
        with self.conn() as c:
            c.execute("INSERT INTO jobs(id,user_id,mode,plan,settings,seconds,priority,created_at)"
                      " VALUES(?,?,?,?,?,?,?,?)",
                      (job_id, user_id, mode, json.dumps(plan, ensure_ascii=False),
                       json.dumps(settings, ensure_ascii=False), float(seconds), int(bool(priority)), time.time()))
        return self.get_job(user_id, job_id)  # type: ignore[return-value]

    def get_job(self, user_id: int, job_id: str) -> dict | None:
        with self.conn() as c:
            return _row(c.execute("SELECT * FROM jobs WHERE id=? AND user_id=?", (job_id, user_id)).fetchone(),
                        JOB_JSON)

    def get_job_any(self, job_id: str) -> dict | None:
        """只給工作程序與排程器使用(不分使用者)。網站的 API 一律用 get_job。"""
        with self.conn() as c:
            return _row(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(), JOB_JSON)

    def list_jobs(self, user_id: int, limit: int = 30) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                             (user_id, limit)).fetchall()
        return [_row(r, JOB_JSON) for r in rows]

    def claim_next_job(self) -> dict | None:
        """依排隊順序(付費優先)領取下一個工作,標成執行中。一次只會有一個執行中的工作(單張 GPU)。"""
        with self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM jobs WHERE status='running'").fetchone():
                c.execute("COMMIT")
                return None
            r = c.execute("SELECT * FROM jobs WHERE status='queued' " + self._QUEUE_ORDER + " LIMIT 1",
                          (time.time() - self.priority_wait_s,)).fetchone()
            if r is None:
                c.execute("COMMIT")
                return None
            c.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?", (time.time(), r["id"]))
            c.execute("COMMIT")
            return _row(c.execute("SELECT * FROM jobs WHERE id=?", (r["id"],)).fetchone(), JOB_JSON)

    def finish_job(self, job_id: str, status: str, error: str = "") -> None:
        with self.conn() as c:
            c.execute("UPDATE jobs SET status=?, error=?, finished_at=? WHERE id=?",
                      (status, error, time.time(), job_id))

    def request_cancel(self, user_id: int, job_id: str) -> str | None:
        """排隊中的工作直接取消;執行中的設旗標,由排程器結束工作程序。回傳新狀態或 None(找不到/已結束)。"""
        with self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT status FROM jobs WHERE id=? AND user_id=?", (job_id, user_id)).fetchone()
            if r is None or r["status"] not in ("queued", "running"):
                c.execute("COMMIT")
                return None
            if r["status"] == "queued":
                c.execute("UPDATE jobs SET status='canceled', cancel=1, finished_at=? WHERE id=?",
                          (time.time(), job_id))
                result = "canceled"
            else:
                c.execute("UPDATE jobs SET cancel=1 WHERE id=?", (job_id,))
                result = "canceling"
            c.execute("COMMIT")
            return result

    def cancel_requested(self, job_id: str) -> bool:
        with self.conn() as c:
            r = c.execute("SELECT cancel FROM jobs WHERE id=?", (job_id,)).fetchone()
            return bool(r and r["cancel"])

    def queue_position(self, job_id: str) -> int:
        """0 = 執行中;1 = 下一個;依此類推。已結束回傳 -1。"""
        with self.conn() as c:
            r = c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if r is None or r["status"] not in ("queued", "running"):
                return -1
            if r["status"] == "running":
                return 0
            ids = [j["id"] for j in self.queued_jobs(c)]
            running = c.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]
            return (ids.index(job_id) if job_id in ids else len(ids)) + running + 1

    def recover_interrupted(self) -> list[dict]:
        """程式重開後,上次還在執行中的工作一定已經中斷(工作程序隨主程式結束):標成失敗並回傳。"""
        with self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = [_row(r, JOB_JSON) for r in c.execute("SELECT * FROM jobs WHERE status='running'")]
            c.execute("UPDATE jobs SET status='failed', error='伺服器重新啟動,工作中斷,請重新送出', finished_at=?"
                      " WHERE status='running'", (time.time(),))
            c.execute("COMMIT")
        return rows

    # ------------------------------------------------------------------ 事件
    def add_event(self, job_id: str, user_id: int, kind: str, data: dict) -> int:
        with self.conn() as c:
            cur = c.execute("INSERT INTO events(job_id,user_id,kind,data,ts) VALUES(?,?,?,?,?)",
                            (job_id, user_id, kind, json.dumps(data, ensure_ascii=False), time.time()))
            return int(cur.lastrowid)

    def count_events(self, job_id: str) -> int:
        with self.conn() as c:
            return c.execute("SELECT COUNT(*) FROM events WHERE job_id=?", (job_id,)).fetchone()[0]

    def events_after(self, user_id: int, job_id: str, after: int, limit: int = 500) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT seq,kind,data,ts FROM events WHERE job_id=? AND user_id=? AND seq>? ORDER BY seq LIMIT ?",
                (job_id, user_id, after, limit)).fetchall()
        return [{"seq": r["seq"], "kind": r["kind"], "data": json.loads(r["data"]), "ts": r["ts"]} for r in rows]
