"""第 4.5 階段:會員等級、帳號核准、個別權限、付費優先排隊、管理頁、埠號設定。"""
import socket
import time

import pytest
from starlette.testclient import TestClient

from tests.web.conftest import Client
from tests.web.test_stage4 import MB, chunked, more_media  # noqa: F401  (more_media 是 fixture)
from tests.web.test_web import _job


@pytest.fixture()
def quiet(env):
    """沒有 GPU 執行器的網站:工作會一直排隊,用來測排隊順序。"""
    from app.web import config as config_mod
    from app.web.api import create_app
    app = create_app(config_mod.load(), with_runner=False)
    with TestClient(app):
        yield app


def _uid(c):
    return c.req("GET", "/api/me").json()["id"]


# ---------------------------------------------------------------------- 等級預設值
def test_tier_defaults(server):
    app = server[0]
    free, paid = Client(app, "f", tier="free"), Client(app, "p", tier="paid")
    uf, up = free.req("GET", "/api/usage").json(), paid.req("GET", "/api/usage").json()
    assert uf["tier"] == "free" and uf["tier_label"] == "一般會員" and uf["priority"] is False
    assert uf["limits"]["audio_bytes"] == 200 * MB and uf["limits"]["original_allowed"] is False
    assert uf["limits"]["duration_s"] == 180 * 60 and uf["daily_limit"] == 120 * 60 and uf["storage_limit"] == 1024 * MB
    assert up["tier"] == "paid" and up["priority"] is True and up["limits"]["original_allowed"] is True
    assert up["limits"]["audio_bytes"] == 300 * MB and up["daily_limit"] == 300 * 60 and up["storage_limit"] == 10240 * MB
    assert uf["limits"]["keep_audio_days"] == 3 and uf["limits"]["keep_srt_days"] == 7
    assert free.req("GET", "/api/me").json()["tier_label"] == "一般會員"


def test_free_member_cannot_upload_original(server, more_media):
    app = server[0]
    free = Client(app, "f", tier="free")
    r = free.req("POST", "/api/uploads", json={"name": "a.mp4", "size": 10 * MB, "content": "original"})
    assert r.status_code == 403 and "請管理者開通改傳原檔" in r.json()["error"]
    assert free.upload(more_media["video"]).status_code == 403            # 整檔上傳的舊路線也擋
    # 音檔、瀏覽器抽好的音訊、字幕都可以
    _, fin = chunked(free, more_media["audio_of_video"], name="會議.mp4", content="audio")
    assert fin.status_code == 200
    assert free.upload(more_media["long"]).status_code == 200
    # 個別開通後就可以
    app.state.db.update_user(_uid(free), overrides={"original_allowed": True})
    _, fin = chunked(free, more_media["video"], content="original")
    assert fin.status_code == 200, fin.text


def test_pending_account_can_do_nothing(server, media_files):
    app = server[0]
    c = TestClient(app)
    c.post("/api/dev-login", json={"name": "newbie", "pending": True})
    me = c.get("/api/me").json()
    assert me["status"] == "pending"
    h = {"X-CSRF-Token": me["csrf"]}
    for method, url in (("GET", "/api/state"), ("GET", "/api/usage"), ("GET", "/api/options"),
                        ("POST", "/api/uploads"), ("POST", "/api/jobs"), ("GET", "/api/glossary"),
                        ("GET", "/api/admin/users")):
        assert c.request(method, url, headers=h, json={}).status_code == 403, url


# ---------------------------------------------------------------------- 管理頁
def test_admin_approve_as_paid_and_edit(server):
    app = server[0]
    boss = Client(app, "boss", admin=True)
    c = TestClient(app)
    c.post("/api/dev-login", json={"name": "new", "pending": True})
    nid = c.get("/api/me").json()["id"]
    data = boss.req("GET", "/api/admin/users").json()
    assert data["users"][0]["id"] == nid and data["users"][0]["status"] == "pending"
    assert {f["key"] for f in data["fields"]} >= {"audio_mb", "original_allowed", "priority", "keep_srt_days"}
    assert data["tiers"]["free"]["daily_minutes"] == 120
    r = boss.req("POST", f"/api/admin/users/{nid}", json={"status": "approved", "tier": "paid"})
    assert r.status_code == 200 and r.json()["tier"] == "paid" and r.json()["perms"]["original_allowed"] is True
    assert c.get("/api/state").status_code == 200
    assert c.get("/api/usage").json()["tier"] == "paid"


def test_overrides_beat_tier_and_can_be_cleared(server):
    app = server[0]
    boss = Client(app, "boss", admin=True)
    a = Client(app, "a", tier="free")
    aid = _uid(a)
    r = boss.req("POST", f"/api/admin/users/{aid}", json={"overrides": {"original_allowed": True, "daily_minutes": 600,
                                                                          "audio_mb": ""}})
    assert r.status_code == 200
    assert r.json()["overrides"] == {"original_allowed": True, "daily_minutes": 600}        # 空白 = 照等級
    u = a.req("GET", "/api/usage").json()
    assert u["limits"]["original_allowed"] is True and u["daily_limit"] == 600 * 60 and u["limits"]["audio_bytes"] == 200 * MB
    # 改等級預設值:立即套用;個別設定過的項目不受影響
    vals = boss.req("GET", "/api/admin/users").json()["tiers"]["free"]
    vals.update(audio_mb=250, daily_minutes=60)
    assert boss.req("PUT", "/api/admin/tiers/free", json=vals).status_code == 200
    u = a.req("GET", "/api/usage").json()
    assert u["limits"]["audio_bytes"] == 250 * MB and u["daily_limit"] == 600 * 60
    # 清除個別設定 = 全部照等級
    boss.req("POST", f"/api/admin/users/{aid}", json={"overrides": {}})
    u = a.req("GET", "/api/usage").json()
    assert u["limits"]["original_allowed"] is False and u["daily_limit"] == 60 * 60


@pytest.mark.parametrize("body,word", [
    ({"overrides": {"audio_mb": -5}}, "之間"),
    ({"overrides": {"audio_mb": "abc"}}, "整數"),
    ({"overrides": {"hack": 1}}, "不認得"),
    ({"overrides": {"priority": "maybe"}}, "開或關"),
    ({"tier": "gold"}, "等級"),
    ({"status": "weird"}, "狀態"),
    ({}, "沒有要修改"),
])
def test_admin_validation(server, body, word):
    app = server[0]
    boss, a = Client(app, "boss", admin=True), Client(app, "a")
    r = boss.req("POST", f"/api/admin/users/{_uid(a)}", json=body)
    assert r.status_code == 400 and word in r.json()["error"], r.text


def test_tier_validation_and_guards(server):
    app = server[0]
    boss, a = Client(app, "boss", admin=True), Client(app, "a")
    vals = boss.req("GET", "/api/admin/users").json()["tiers"]["paid"]
    bad = dict(vals); bad.pop("priority")
    assert boss.req("PUT", "/api/admin/tiers/paid", json=bad).status_code == 400
    assert boss.req("PUT", "/api/admin/tiers/gold", json=vals).status_code == 404
    assert a.req("PUT", "/api/admin/tiers/paid", json=vals).status_code == 403             # 不是管理者
    assert a.req("GET", "/api/admin/queue").status_code == 403
    bid = _uid(boss)
    assert boss.req("POST", f"/api/admin/users/{bid}", json={"is_admin": False}).status_code == 400
    assert boss.req("POST", f"/api/admin/users/{bid}", json={"status": "blocked"}).status_code == 400
    assert boss.req("POST", f"/api/admin/users/{bid}", json={"overrides": {"storage_mb": 20000}}).status_code == 200
    # 指定別人當管理者
    assert boss.req("POST", f"/api/admin/users/{_uid(a)}", json={"is_admin": True}).status_code == 200
    assert a.req("GET", "/api/admin/users").status_code == 200


def test_queue_limit_from_perms(quiet, media_files):
    a = Client(quiet, "a", tier="free")
    quiet.state.db.update_user(_uid(a), overrides={"queued_jobs": 1})
    i1, i2 = a.upload(media_files["a"]).json(), a.upload(media_files["b"]).json()
    assert _job(a, [i1["id"]]).status_code == 200
    r = _job(a, [i2["id"]])
    assert r.status_code == 400 and "1 個" in r.json()["error"]


def test_translation_permissions(quiet, media_files):
    a = Client(quiet, "a", tier="free")
    uid = _uid(a)
    quiet.state.db.update_user(uid, overrides={"offline_translate": False})
    o = a.req("GET", "/api/options").json()
    assert "m2m100" not in o["engines"] and o["engines"] == ["anthropic", "openai"]
    quiet.state.db.update_user(uid, overrides={"offline_translate": False, "api_translate": False})
    o = a.req("GET", "/api/options").json()
    assert o["engines"] == [] and o["targets"] == []
    it = a.upload(media_files["a"]).json()
    r = _job(a, [it["id"]], translate_to="en")
    assert r.status_code == 400 and "沒有開通翻譯" in r.json()["error"]
    assert _job(a, [it["id"]]).status_code == 200                       # 只轉錄可以
    # 存過的偏好被收回權限時,不影響送出工作
    quiet.state.db.update_user(uid, overrides={"offline_translate": True, "api_translate": False},
                               prefs={"translation_engine": "anthropic"})
    assert a.req("PUT", "/api/prefs", json={"vad": False}).status_code == 200


# ---------------------------------------------------------------------- 付費優先排隊
def test_priority_order_in_db(tmp_path):
    from app.web.db import DB
    db = DB(tmp_path / "q.sqlite3", priority_wait_s=1800)
    uid = db.upsert_user("dev", "x", None, "x", status="approved")["id"]
    now = time.time()
    for jid, pri, age in (("free_old", 0, 3600), ("free_new", 0, 60), ("paid_a", 1, 30), ("paid_b", 1, 10)):
        db.create_job(uid, jid, "auto", [], {}, priority=bool(pri))
        with db.conn() as c:
            c.execute("UPDATE jobs SET created_at=? WHERE id=?", (now - age, jid))
    # free_old 已經等超過 30 分鐘,和優先的一起依送出時間排,所以排第一
    assert [j["id"] for j in db.queued_jobs()] == ["free_old", "paid_a", "paid_b", "free_new"]
    assert [db.queue_position(j) for j in ("free_old", "paid_a", "paid_b", "free_new")] == [1, 2, 3, 4]
    order = []
    while (j := db.claim_next_job()) is not None:
        order.append(j["id"])
        assert db.queue_position(j["id"]) == 0
        db.finish_job(j["id"], "done")
    assert order == ["free_old", "paid_a", "paid_b", "free_new"]


def test_paid_jumps_queue_via_api(quiet, media_files):
    free, paid = Client(quiet, "f", tier="free"), Client(quiet, "p", tier="paid")
    jf = _job(free, [free.upload(media_files["a"]).json()["id"]]).json()["job"]
    jp = _job(paid, [paid.upload(media_files["b"]).json()["id"]]).json()["job"]
    assert free.req("GET", f"/api/jobs/{jf['id']}").json()["position"] == 2
    assert paid.req("GET", f"/api/jobs/{jp['id']}").json()["position"] == 1
    boss = Client(quiet, "boss", admin=True)
    q = boss.req("GET", "/api/admin/queue").json()
    assert [j["user"] for j in q["jobs"]] == ["p", "f"] and q["jobs"][0]["priority"] is True
    assert q["priority_max_wait_min"] == 30
    assert "a.mp3" not in str(q) and "b.mp3" not in str(q)             # 不顯示檔名
    assert quiet.state.db.claim_next_job()["id"] == jp["id"]


# ---------------------------------------------------------------------- 其他
def test_last_seen_and_migration(server, tmp_path):
    app = server[0]
    a = Client(app, "a")
    uid = _uid(a)
    app.state.db.update_user(uid, name="a")
    with app.state.db.conn() as c:
        c.execute("UPDATE users SET last_seen=0 WHERE id=?", (uid,))
    a.req("GET", "/api/state")
    assert app.state.db.get_user(uid)["last_seen"] > time.time() - 60
    # 第 4 階段的資料庫:補上欄位,舊帳號維持可使用、一般會員
    import sqlite3
    from app.web.db import DB
    p = tmp_path / "v3.sqlite3"
    c = sqlite3.connect(p)
    c.executescript("""CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, subject TEXT NOT NULL,
        email TEXT, name TEXT, status TEXT NOT NULL DEFAULT 'pending', tier TEXT NOT NULL DEFAULT 'free',
        is_admin INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, prefs TEXT NOT NULL DEFAULT '{}',
        UNIQUE(provider, subject));
        CREATE TABLE jobs (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
        mode TEXT NOT NULL, plan TEXT NOT NULL, settings TEXT NOT NULL, cancel INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, started_at REAL, finished_at REAL);
        INSERT INTO users(provider,subject,name,status,created_at) VALUES('google','old','old','approved',0);
        INSERT INTO jobs(id,user_id,mode,plan,settings,created_at) VALUES('j','1','auto','[]','{}',0);""")
    c.commit(); c.close()
    db = DB(p)
    u = db.get_user(1)
    assert u["status"] == "approved" and u["tier"] == "free" and u["overrides"] == {}
    assert db.get_job_any("j")["priority"] == 0 and db.get_tiers() == {}
    from app.web import perms
    assert perms.effective(db, u)["daily_minutes"] == 120


def test_port_settings(env):
    from app.web import config as config_mod
    from app.web.__main__ import browser_url, port_in_use
    env.delenv("WEB_PORT", raising=False)
    env.delenv("WEB_PUBLIC_URL", raising=False)
    cfg = config_mod.load()
    assert cfg.port == 8780 and cfg.public_url == "http://localhost:8780"
    env.setenv("WEB_PORT", "8999")
    cfg = config_mod.load()
    assert cfg.port == 8999 and cfg.public_url == "http://localhost:8999"
    assert browser_url("http://localhost:8999", 8999) == "http://localhost:8999/"
    assert browser_url("http://localhost:8000", 8999) == "http://localhost:8999/"     # .env 的網址沒跟著改
    assert browser_url("https://sub.example.com", 8999) == "http://localhost:8999/"
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert port_in_use("127.0.0.1", port) is True
    finally:
        s.close()
    assert port_in_use("127.0.0.1", port) is False


def test_port_in_use_message(env, capsys):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    env.setenv("WEB_PORT", str(port))
    from app.web.__main__ import main
    try:
        assert main([]) == 2
    finally:
        s.close()
    out = capsys.readouterr().out
    assert f"{port} 已被其他程式使用,請在 .env 改 WEB_PORT" in out
