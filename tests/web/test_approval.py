"""審核機制(10/8 決定加回):新 Google 帳號等管理者核准;管理頁可核准、拒絕、停用、恢復。"""
from starlette.testclient import TestClient

from tests.web.test_google import BASE, _info, _login, gapp  # noqa: F401  (gapp 是 fixture)


def test_approval_is_on_by_default(env):
    env.delenv("WEB_REQUIRE_APPROVAL", raising=False)
    from app.web import config as config_mod
    assert config_mod.load().require_approval is True


def _admin_and_newbie(gapp):
    app, calls = gapp(WEB_REQUIRE_APPROVAL="1", WEB_ADMIN_EMAILS="boss@example.com")
    boss, _ = _login(app, calls, _info(sub="boss", email="boss@example.com", name="Boss"))
    newbie, _ = _login(app, calls, _info(sub="new", email="new@gmail.com", name="New"))
    hb = {"X-CSRF-Token": boss.get("/api/me").json()["csrf"]}
    return app, calls, boss, hb, newbie


def test_pending_then_approved(gapp):
    app, calls, boss, hb, newbie = _admin_and_newbie(gapp)
    me = newbie.get("/api/me").json()
    assert me["status"] == "pending" and me["email"] == "new@gmail.com"
    assert newbie.get("/api/state").status_code == 403
    users = boss.get("/api/admin/users").json()
    assert users["require_approval"] is True
    assert [u["email"] for u in users["users"]][0] == "new@gmail.com"            # 等待核准的排最前面
    nid = me["id"]
    assert boss.post(f"/api/admin/users/{nid}", json={"status": "approved"}, headers=hb).status_code == 200
    assert newbie.get("/api/state").status_code == 200                           # 不用重新登入
    # 再次登入不會被打回待核准
    again, _ = _login(app, calls, _info(sub="new", email="new@gmail.com"))
    assert again.get("/api/me").json()["status"] == "approved"


def test_reject_and_restore(gapp):
    app, calls, boss, hb, newbie = _admin_and_newbie(gapp)
    nid = newbie.get("/api/me").json()["id"]
    boss.post(f"/api/admin/users/{nid}", json={"status": "blocked"}, headers=hb)
    assert newbie.get("/api/me").json()["status"] == "blocked"
    _, r = _login(app, calls, _info(sub="new", email="new@gmail.com"))
    assert r.headers["location"] == "/?login_error=blocked"
    boss.post(f"/api/admin/users/{nid}", json={"status": "approved"}, headers=hb)
    assert newbie.get("/api/state").status_code == 200


def test_admin_only_and_guards(gapp):
    app, calls, boss, hb, newbie = _admin_and_newbie(gapp)
    nid = newbie.get("/api/me").json()["id"]
    boss.post(f"/api/admin/users/{nid}", json={"status": "approved"}, headers=hb)
    hn = {"X-CSRF-Token": newbie.get("/api/me").json()["csrf"]}
    assert newbie.get("/api/admin/users").status_code == 403                      # 一般使用者不能看
    assert newbie.post(f"/api/admin/users/{nid}", json={"status": "approved"}, headers=hn).status_code == 403
    bid = boss.get("/api/me").json()["id"]
    assert boss.post(f"/api/admin/users/{bid}", json={"status": "blocked"}, headers=hb).status_code == 400  # 不能停用自己
    assert boss.post(f"/api/admin/users/{nid}", json={"status": "weird"}, headers=hb).status_code == 400
    assert boss.post("/api/admin/users/9999", json={"status": "approved"}, headers=hb).status_code == 404
    assert boss.post(f"/api/admin/users/{nid}", json={"status": "blocked"}).status_code == 403  # 沒有 CSRF 權杖
    assert TestClient(app, base_url=BASE).get("/api/admin/users").status_code == 401


def test_block_cancels_jobs(server, alice, media_files, env):
    from tests.web.conftest import Client
    from tests.web.test_web import _job
    app, cfg = server
    env.setenv("FAKE_SLEEP", "0.5")
    env.setenv("FAKE_STEPS", "30")
    boss = Client(app, "boss")
    app.state.db.update_user(boss.c.get("/api/me").json()["id"], is_admin=1)
    a = alice.upload(media_files["a"]).json()
    b = alice.upload(media_files["b"]).json()
    j1 = _job(alice, [a["id"]]).json()["job"]["id"]
    j2 = _job(alice, [b["id"]]).json()["job"]["id"]
    alice.wait_job(j1, want=("running",))
    aid = alice.req("GET", "/api/me").json()["id"]
    assert boss.req("POST", f"/api/admin/users/{aid}", json={"status": "blocked"}).status_code == 200
    db = app.state.db
    import time
    t = time.time()
    while time.time() - t < 20 and db.get_job(aid, j1)["status"] not in ("canceled", "done"):
        time.sleep(0.1)
    assert db.get_job(aid, j1)["status"] == "canceled"
    assert db.get_job(aid, j2)["status"] == "canceled"
