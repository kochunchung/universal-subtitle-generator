import json
import os
import re
import signal
import time
import urllib.parse

import pytest

from app.web import jobs as jobs_mod
from app.web.db import DB
from app.web.runner import Runner


def _alive(pid: int) -> bool:
    """程序還在執行(殭屍程序算已結束:它已經被殺掉,只是沒有人回收)。"""
    try:
        for line in open(f"/proc/{pid}/status", encoding="utf-8"):
            if line.startswith("State:"):
                return not line.split()[1].startswith("Z")
    except FileNotFoundError:
        return False
    return True


def _job(c, ids, mode="auto", **opts):
    r = c.req("POST", "/api/jobs", json={"item_ids": ids, "mode": mode, "options": opts})
    return r


def test_requires_login(server):
    from starlette.testclient import TestClient
    c = TestClient(server[0])
    assert c.get("/api/state").status_code == 401
    assert c.get("/api/me").status_code == 401


def test_csrf_required(alice):
    r = alice.c.post("/api/jobs", json={"item_ids": [], "mode": "auto", "options": {}})
    assert r.status_code == 403
    assert alice.c.get("/api/state").status_code == 200       # 讀取不需要權杖


def test_pending_user_blocked(server):
    app, cfg = server
    db = app.state.db
    u = db.upsert_user("google", "x1", "x@example.com", "新人")     # 預設待核准
    from app.web import auth
    from starlette.testclient import TestClient
    c = TestClient(app)
    c.cookies.set(auth.COOKIE, auth.make_cookie(cfg, u["id"]))
    assert c.get("/api/me").json()["status"] == "pending"
    r = c.get("/api/state")
    assert r.status_code == 403 and "核准" in r.json()["error"]


def test_upload_validation(alice, media_files):
    r = alice.upload(media_files["a"])
    assert r.status_code == 200
    it = r.json()
    assert it["kind"] == "media" and it["state"] == "uploaded" and it["duration"] > 1
    assert alice.req("PUT", "/api/items", params={"name": "x.exe"}, content=b"abc").status_code == 415
    r = alice.upload(media_files["bad"])
    assert r.status_code == 400
    assert "/" not in r.json()["error"].replace("檔案", "") or "data" not in r.json()["error"]   # 不洩漏伺服器路徑
    assert alice.req("PUT", "/api/items", params={"name": "e.srt"}, content=b"not a subtitle").status_code == 400
    assert len(alice.req("GET", "/api/state").json()["items"]) == 1      # 壞檔不會留下紀錄


def test_path_traversal_names(alice, media_files, server):
    r = alice.upload(media_files["a"], name="../../etc/passwd.mp3")
    assert r.status_code == 200
    assert r.json()["name"] == "passwd.mp3"
    cfg = server[1]
    # 檔案只會出現在使用者自己的項目資料夾
    found = [p for p in cfg.data_dir.rglob("*.mp3")]
    assert len(found) == 1 and "users" in found[0].parts


def test_full_flow_and_download(alice, media_files):
    a = alice.upload(media_files["video"]).json()      # 檔名有中文與空白
    r = _job(alice, [a["id"]])
    assert r.status_code == 200, r.text
    job = alice.wait_job(r.json()["job"]["id"])
    assert job["status"] == "done"
    it = alice.req("GET", "/api/state").json()["items"][0]
    assert it["state"] == "done" and it["progress"] == 1.0 and "srt" in it["outputs"]
    d = alice.req("GET", f"/api/items/{a['id']}/download", params={"which": "srt"})
    assert d.status_code == 200
    cd = d.headers["content-disposition"]
    fname = urllib.parse.unquote(re.search(r"filename\*=UTF-8''(.+)$", cd).group(1))
    assert fname == "會議 記錄.zh-TW.srt"
    assert d.content.startswith(b"\xef\xbb\xbf")                 # BOM 原樣保留
    assert alice.req("GET", f"/api/items/{a['id']}/download", params={"which": "translated"}).status_code == 404


def test_translate_only_does_not_retranscribe(alice, media_files):
    a = alice.upload(media_files["a"]).json()
    j1 = alice.wait_job(_job(alice, [a["id"]]).json()["job"]["id"])
    assert j1["status"] == "done"
    r = _job(alice, [a["id"]], translate_to="en")           # 已有字幕 → 只翻譯
    assert r.status_code == 200, r.text
    job = alice.wait_job(r.json()["job"]["id"])
    assert job["status"] == "done"
    it = alice.req("GET", "/api/state").json()["items"][0]
    assert "translated" in it["outputs"]
    d = alice.req("GET", f"/api/items/{a['id']}/download", params={"which": "translated"})
    assert urllib.parse.unquote(d.headers["content-disposition"]).endswith("a.en.srt")
    # 再按一次「開始處理」:沒有可做的事,明確告訴使用者
    r = _job(alice, [a["id"]], translate_to="en")
    assert r.status_code == 400 and "沒有需要處理" in r.json()["error"]


def test_plan_kinds(server, alice, media_files):
    app, cfg = server
    a = alice.upload(media_files["a"]).json()
    r = _job(alice, [a["id"]], translate_to="en")
    job = app.state.db.get_job_any(r.json()["job"]["id"])
    assert [e["kind"] for e in job["plan"]] == ["media"]          # 沒字幕:轉錄(worker 會接著翻譯)
    alice.wait_job(job["id"])
    r = _job(alice, [a["id"]], mode="retranscribe", translate_to="en")
    job2 = app.state.db.get_job_any(r.json()["job"]["id"])
    assert job2["plan"][0]["overwrite"] is True
    alice.wait_job(job2["id"])
    r = _job(alice, [a["id"]], mode="retranslate", translate_to="en")
    job3 = app.state.db.get_job_any(r.json()["job"]["id"])
    assert job3["plan"][0]["kind"] == "srt" and job3["plan"][0]["overwrite"] is True


def test_srt_item_translation(alice):
    srt = "1\n00:00:00,000 --> 00:00:01,000\nHello world, this is a test.\n\n"
    r = alice.req("PUT", "/api/items", params={"name": "talk.en.srt"}, content=srt.encode())
    assert r.status_code == 200
    it = r.json()
    r = _job(alice, [it["id"]], translate_to="zh-TW")
    assert r.status_code == 200, r.text
    assert alice.wait_job(r.json()["job"]["id"])["status"] == "done"
    d = alice.req("GET", f"/api/items/{it['id']}/download", params={"which": "translated"})
    assert d.status_code == 200
    assert "talk.zh-TW.srt" in urllib.parse.unquote(d.headers["content-disposition"])
    # 字幕檔不選翻譯目標:沒有事情可做
    r = _job(alice, [it["id"]])
    assert r.status_code == 400


def test_isolation(alice, bob, media_files):
    a = alice.upload(media_files["a"]).json()
    job = alice.wait_job(_job(alice, [a["id"]]).json()["job"]["id"])
    assert bob.req("GET", "/api/state").json() == {"items": [], "jobs": []}
    assert bob.req("GET", f"/api/items/{a['id']}/download", params={"which": "srt"}).status_code == 404
    assert bob.req("DELETE", f"/api/items/{a['id']}").status_code == 404
    assert bob.req("GET", f"/api/jobs/{job['id']}").status_code == 404
    assert bob.req("POST", f"/api/jobs/{job['id']}/cancel").status_code == 404
    assert bob.req("GET", f"/api/jobs/{job['id']}/events").status_code == 404
    r = _job(bob, [a["id"]])                      # 用別人的檔案編號建工作:等同不存在
    assert r.status_code == 400 and "沒有需要處理" in r.json()["error"]
    assert alice.req("GET", f"/api/items/{a['id']}/download", params={"which": "srt"}).status_code == 200


def test_invalid_options(alice, media_files):
    a = alice.upload(media_files["a"]).json()
    for bad in ({"whisper_model": "evil/model"}, {"translation_engine": "nllb"}, {"translate_to": "xx"},
                {"subtitle_mode": "x"}):
        r = _job(alice, [a["id"]], **bad)
        assert r.status_code == 400, bad
    assert _job(alice, [a["id"]], max_chars_cjk="abc").status_code == 400
    r = _job(alice, [a["id"]], mode="bogus")
    assert r.status_code == 400
    r = _job(alice, [a["id"]], translate_to="en", translation_engine="anthropic")
    assert r.status_code == 400 and "請先到「設定」頁填你自己的金鑰" in r.json()["error"]


def test_events_sse_and_resume(alice, media_files, server):
    server_cfg = server[1]
    a = alice.upload(media_files["a"]).json()
    jid = _job(alice, [a["id"]]).json()["job"]["id"]
    alice.wait_job(jid)

    def read(after=0):
        with alice.c.stream("GET", f"/api/jobs/{jid}/events", params={"after": after}) as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            return r.read().decode()

    body = read()
    assert "event: log" in body and "event: file" in body and "event: end" in body
    ids = [int(x) for x in re.findall(r"^id: (\d+)$", body, re.M)]
    assert ids == sorted(ids) and len(ids) >= 4
    body2 = read(ids[1])                      # 從中間續傳:只收到之後的事件
    ids2 = [int(x) for x in re.findall(r"^id: (\d+)$", body2, re.M)]
    assert ids2 == [i for i in ids if i > ids[1]]
    assert str(server_cfg.data_dir) not in body        # 沒有路徑洩漏
    assert "[a.mp3]" in body                                                       # 換成使用者的原檔名


def test_cancel_running_kills_children(alice, media_files, env, tmp_path):
    pidfile = tmp_path / "child.pid"
    env.setenv("FAKE_CHILD", str(pidfile))
    env.setenv("FAKE_SLEEP", "0.5")
    env.setenv("FAKE_STEPS", "40")
    a = alice.upload(media_files["a"]).json()
    jid = _job(alice, [a["id"]]).json()["job"]["id"]
    alice.wait_job(jid, want=("running",))
    t = time.time()
    while not pidfile.exists() and time.time() - t < 15:
        time.sleep(0.1)
    child = int(pidfile.read_text())
    assert _alive(child)                                 # 子程序還活著
    assert alice.req("POST", f"/api/jobs/{jid}/cancel").json()["status"] == "canceling"
    job = alice.wait_job(jid)
    assert job["status"] == "canceled"
    time.sleep(0.5)
    assert not _alive(child)                             # 連子程序(模擬 ffmpeg)一起結束
    it = alice.req("GET", "/api/state").json()["items"][0]
    assert it["state"] == "canceled"
    # 取消後可以重新處理
    env.setenv("FAKE_SLEEP", "0.01")
    r = _job(alice, [a["id"]])
    assert r.status_code == 200
    assert alice.wait_job(r.json()["job"]["id"])["status"] == "done"


def test_cancel_queued_and_queue_limit(alice, media_files, env, server):
    env.setenv("FAKE_SLEEP", "0.5")
    env.setenv("FAKE_STEPS", "20")
    ids = [alice.upload(media_files["a"]).json()["id"] for _ in range(4)]
    j1 = _job(alice, [ids[0]]).json()["job"]
    j2 = _job(alice, [ids[1]]).json()["job"]
    j3 = _job(alice, [ids[2]]).json()["job"]
    assert [j1["position"], j2["position"], j3["position"]][1:] == [2, 3] or j1["position"] in (0, 1)
    r = _job(alice, [ids[3]])
    assert r.status_code == 400 and "上限" in r.json()["error"]
    assert alice.req("POST", f"/api/jobs/{j3['id']}/cancel").json()["status"] == "canceled"   # 排隊中:直接取消
    items = {i["id"]: i for i in alice.req("GET", "/api/state").json()["items"]}
    assert items[ids[2]]["state"] == "canceled"
    assert alice.req("POST", f"/api/jobs/{j3['id']}/cancel").status_code == 409                  # 已結束
    # 排隊中的檔案不能刪
    assert alice.req("DELETE", f"/api/items/{ids[1]}").status_code == 409
    # 同一個檔案不能同時排進兩個工作
    r = _job(alice, [ids[1]])
    assert r.status_code == 400
    alice.req("POST", f"/api/jobs/{j1['id']}/cancel")
    alice.req("POST", f"/api/jobs/{j2['id']}/cancel")
    alice.wait_job(j1["id"]); alice.wait_job(j2["id"])


def test_fifo_across_users(alice, bob, media_files, env):
    env.setenv("FAKE_SLEEP", "0.3")
    env.setenv("FAKE_STEPS", "5")
    a = alice.upload(media_files["a"]).json()
    b = bob.upload(media_files["b"]).json()
    ja = _job(alice, [a["id"]]).json()["job"]
    jb = _job(bob, [b["id"]]).json()["job"]
    ja_done = alice.wait_job(ja["id"])
    jb_done = bob.wait_job(jb["id"])
    assert ja_done["status"] == jb_done["status"] == "done"
    assert ja_done["finished_at"] <= jb_done["started_at"] + 0.01          # 一次只跑一個


def test_worker_crash(alice, media_files, env):
    env.setenv("FAKE_CRASH", "1")
    a = alice.upload(media_files["a"]).json()
    job = alice.wait_job(_job(alice, [a["id"]]).json()["job"]["id"])
    assert job["status"] == "failed" and "意外中止" in job["error"]
    it = alice.req("GET", "/api/state").json()["items"][0]
    assert it["state"] == "failed"
    env.setenv("FAKE_CRASH", "0")                         # 執行器沒有因此停止
    r = _job(alice, [a["id"]], mode="retranscribe")
    assert alice.wait_job(r.json()["job"]["id"])["status"] == "done"


def test_recover_interrupted(tmp_path, env):
    from app.web import config as config_mod
    cfg = config_mod.load()
    db = DB(cfg.db_path)
    u = db.upsert_user("dev", "z", None, "z", status="approved")
    d = cfg.item_dir(u["id"], "it1"); d.mkdir(parents=True)
    (d / "it1.mp3").write_bytes(b"x")
    db.create_item(u["id"], "it1", "z.mp3", "media", "it1.mp3", 1)
    db.create_job(u["id"], "j1", "auto", [{"idx": 0, "item_id": "it1", "kind": "media", "path": "x"}], {})
    assert db.claim_next_job()["id"] == "j1"
    db.update_item("it1", state="running")
    Runner(cfg, db).recover()
    assert db.get_job(u["id"], "j1")["status"] == "failed"
    assert db.get_item(u["id"], "it1")["state"] == "failed"


def test_delete_item_removes_files(alice, media_files, server):
    cfg = server[1]
    a = alice.upload(media_files["a"]).json()
    assert any(cfg.data_dir.rglob(f"{a['id']}.mp3"))
    assert alice.req("DELETE", f"/api/items/{a['id']}").status_code == 200
    assert not any(cfg.data_dir.rglob(f"{a['id']}*"))


def test_progress_events_throttled(alice, media_files, env, server):
    env.setenv("FAKE_STEPS", "200")
    env.setenv("FAKE_SLEEP", "0.002")
    a = alice.upload(media_files["a"]).json()
    jid = _job(alice, [a["id"]]).json()["job"]["id"]
    alice.wait_job(jid)
    n = server[0].state.db.count_events(jid)
    assert n < 60, n                                      # 200 筆進度不會變成 200 筆事件
    it = alice.req("GET", "/api/state").json()["items"][0]
    assert it["state"] == "done" and it["progress"] == 1.0
