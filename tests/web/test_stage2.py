"""第 2 階段:網頁介面需要的 API(偏好、詞彙表、預覽、打包下載、刪除我的資料)。"""
import io
import re
import time
import zipfile

from tests.web.test_web import _job

SRT = ("1\n00:00:00,000 --> 00:00:02,000\nGood morning everyone, welcome to the meeting.\n\n"
       "2\n00:00:02,000 --> 00:00:02,300\nOK\n\n"
       "3\n00:00:03,000 --> 00:00:06,000\nLet us review the schedule.\n")


def _events_text(c, jid):
    with c.c.stream("GET", f"/api/jobs/{jid}/events") as r:
        return r.read().decode()


def test_pages_and_public_info(server):
    from starlette.testclient import TestClient
    c = TestClient(server[0])
    r = c.get("/")
    assert r.status_code == 200 and "萬能字幕產生器" in r.text and "/static/app.js" in r.text
    assert c.get("/static/app.js").status_code == 200
    assert c.get("/dev", follow_redirects=False).status_code in (302, 303, 307)
    assert c.get("/api/public-info").json() == {"dev_login": True, "google": False}


def test_prefs_validate_clamp_and_apply(alice, media_files):
    assert alice.req("GET", "/api/prefs").json() == {}
    r = alice.req("PUT", "/api/prefs", json={"translate_to": "en", "max_chars_cjk": 99, "bogus": 1})
    assert r.status_code == 200
    assert r.json() == {"translate_to": "en", "max_chars_cjk": 40}          # 夾到上限、未知的鍵丟掉
    assert alice.req("PUT", "/api/prefs", json={"translate_to": "xx"}).status_code == 400
    assert alice.req("PUT", "/api/prefs", json={"translation_engine": "nllb"}).status_code == 400
    a = alice.upload(media_files["a"]).json()
    assert a["duration"] > 1
    jid = _job(alice, [a["id"]], translate_to=None).json()["job"]["id"]      # 這次送出的選項優先
    alice.wait_job(jid)
    body = _events_text(alice, jid)
    assert "cjk=40" in body                                                  # 偏好裡的每行字數有帶進工作


def test_glossary_per_user(alice, bob, media_files, server):
    cfg = server[1]
    assert alice.req("GET", "/api/glossary").json() == {"text": ""}
    r = alice.req("PUT", "/api/glossary", json={"text": "熱軋=hot rolling\r\n中鴻\n# 註解"})
    assert r.json() == {"ok": True, "terms": 2, "fixed": 1}
    assert bob.req("GET", "/api/glossary").json() == {"text": ""}           # 互相看不到
    assert alice.req("PUT", "/api/glossary", json={"text": "x" * 70000}).status_code == 413
    a = alice.upload(media_files["a"]).json()
    jid = _job(alice, [a["id"]]).json()["job"]["id"]
    alice.wait_job(jid)
    me = alice.req("GET", "/api/me").json()
    assert f"glossary-file={me['id']}/glossary.txt" in _events_text(alice, jid)   # 工作程序用的是自己的詞彙表


def test_glossary_env_and_default(tmp_path, monkeypatch):
    from app.translate import glossary
    monkeypatch.delenv("APP_GLOSSARY_FILE", raising=False)
    assert glossary.load().terms == glossary.load(glossary.GLOSSARY_FILE).terms   # 桌面版行為不變
    f = tmp_path / "g.txt"
    f.write_text("甲=A\n乙\n", encoding="utf-8")
    monkeypatch.setenv("APP_GLOSSARY_FILE", str(f))
    g = glossary.load()
    assert g.terms == ["甲", "乙"] and g.translations == {"甲": "A"}
    monkeypatch.setenv("APP_GLOSSARY_FILE", str(tmp_path / "none.txt"))
    assert glossary.load().terms == []                                       # 沒建立過 = 空的,不會用到桌面版的


def test_preview_flags_and_isolation(alice, bob):
    it = alice.req("PUT", "/api/items", params={"name": "talk.en.srt"}, content=SRT.encode()).json()
    r = alice.req("GET", f"/api/items/{it['id']}/preview", params={"which": "srt"})
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 3 and d["flagged"] == 2 and d["name"] == "talk.en.srt"
    assert d["cues"][0]["flags"] == ["字數相對時間過多"]
    assert set(d["cues"][1]["flags"]) == {"顯示時間很短"}
    assert d["cues"][2]["flags"] == []
    assert alice.req("GET", f"/api/items/{it['id']}/preview", params={"which": "translated"}).status_code == 404
    assert alice.req("GET", f"/api/items/{it['id']}/preview", params={"which": "x"}).status_code == 400
    assert bob.req("GET", f"/api/items/{it['id']}/preview", params={"which": "srt"}).status_code == 404


def test_zip_download(alice, bob, media_files):
    a = alice.upload(media_files["a"]).json()
    b = alice.upload(media_files["video"]).json()
    alice.wait_job(_job(alice, [a["id"], b["id"]], translate_to="en").json()["job"]["id"])
    r = alice.req("GET", "/api/download-zip", params={"ids": f"{a['id']},{b['id']}"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    names = sorted(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
    assert names == ["a.en.srt", "a.zh-TW.srt", "會議 記錄.en.srt", "會議 記錄.zh-TW.srt"]
    # 別人的編號不會被打包
    assert bob.req("GET", "/api/download-zip", params={"ids": f"{a['id']},{b['id']}"}).status_code == 404


def test_delete_my_data(alice, bob, media_files, server, env):
    cfg = server[1]
    a = alice.upload(media_files["a"]).json()
    bob.upload(media_files["b"])
    alice.req("PUT", "/api/glossary", json={"text": "甲=A"})
    alice.req("PUT", "/api/prefs", json={"translate_to": "en"})
    env.setenv("FAKE_SLEEP", "0.4")
    env.setenv("FAKE_STEPS", "30")
    jid = _job(alice, [a["id"]], translate_to=None).json()["job"]["id"]
    assert alice.req("DELETE", "/api/my-data").status_code == 409            # 處理中不能刪
    alice.req("POST", f"/api/jobs/{jid}/cancel")
    alice.wait_job(jid)
    assert alice.req("DELETE", "/api/my-data").status_code == 200
    me = alice.req("GET", "/api/me").json()
    assert alice.req("GET", "/api/state").json() == {"items": [], "jobs": []}
    assert alice.req("GET", "/api/prefs").json() == {}
    assert alice.req("GET", "/api/glossary").json() == {"text": ""}
    assert not cfg.user_dir(me["id"]).exists()
    assert len(bob.req("GET", "/api/state").json()["items"]) == 1           # 別人的資料不受影響


def test_wait_estimate(alice, bob, media_files, env):
    env.setenv("FAKE_SLEEP", "0.4")
    env.setenv("FAKE_STEPS", "20")
    a = alice.upload(media_files["video"]).json()
    b = bob.upload(media_files["b"]).json()
    ja = _job(alice, [a["id"]]).json()["job"]
    alice.wait_job(ja["id"], want=("running",))
    jb = bob.req("GET", f"/api/jobs/{_job(bob, [b['id']]).json()['job']['id']}").json()
    assert jb["status"] == "queued" and jb["position"] == 2
    assert isinstance(jb["wait_seconds"], int) and jb["wait_seconds"] >= 20
    alice.req("POST", f"/api/jobs/{ja['id']}/cancel")
    bob.wait_job(jb["id"])


def test_old_database_is_migrated(tmp_path):
    import sqlite3
    from app.web.db import DB
    p = tmp_path / "old.sqlite3"
    c = sqlite3.connect(p)
    c.executescript("""CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, subject TEXT NOT NULL,
        email TEXT, name TEXT, status TEXT NOT NULL DEFAULT 'pending', tier TEXT NOT NULL DEFAULT 'free',
        is_admin INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, UNIQUE(provider, subject));
        INSERT INTO users(provider,subject,name,created_at) VALUES('dev','old','old',0);""")
    c.commit(); c.close()
    db = DB(p)
    u = db.get_user(1)
    assert u["prefs"] == {} and u["name"] == "old"


def test_worker_error_is_failure_not_done(alice, media_files, env):
    """worker 捕捉到例外後仍會送 done:網頁版要顯示失敗,而且不能把伺服器路徑與完整錯誤追蹤給使用者看。"""
    from app.paths import ROOT
    env.setenv("FAKE_RAISE", "1")
    a = alice.upload(media_files["a"]).json()
    b = alice.upload(media_files["b"]).json()
    job = alice.wait_job(_job(alice, [a["id"], b["id"]], translate_to="en").json()["job"]["id"])
    assert job["status"] == "failed"
    assert "device-side assert" in job["error"]
    states = {i["id"]: i["state"] for i in alice.req("GET", "/api/state").json()["items"]}
    assert states[a["id"]] == "failed" and states[b["id"]] == "failed"     # 沒處理到的檔案也標成失敗
    body = _events_text(alice, job["id"])
    assert str(ROOT) not in body and "Traceback" not in body
    assert "工作程序發生錯誤" in body and "RuntimeError: CUDA error: device-side assert triggered" in body


def test_fit_decoder():
    from app.core.transcriber import fit_decoder
    short, long_ = list(range(10)), list(range(600))
    out = fit_decoder([short, long_], 448, reserved=5)
    assert out[0] == short                                   # 正常長度完全不變
    assert len(out[1]) == 443 and out[1] == long_[:443]      # 4 個前綴 + 443 + 結束符號 = 448
