"""第 5 階段:每人 API 金鑰(加密、驗證、只交給工作程序)、到期清除、付費會員自選上傳方式。"""
import time
import urllib.error

import pytest
from starlette.testclient import TestClient

from tests.web.conftest import Client
from tests.web.test_stage4 import chunked, more_media  # noqa: F401  (more_media 是 fixture)
from tests.web.test_stage45 import _uid, quiet  # noqa: F401  (quiet 是 fixture)
from tests.web.test_web import _job

KEY_A = "sk-ant-api03-" + "A" * 40 + "WXYZ"
KEY_O = "sk-proj-" + "B" * 40 + "9876"
DAY = 86400


@pytest.fixture()
def verified(monkeypatch):
    """不連外:記錄被驗證的金鑰;金鑰含 BAD 時當作無效。"""
    from app.web import apikeys
    calls = []

    def fake(provider, key, model=""):
        calls.append((provider, key, model))
        if "BAD" in key:
            raise apikeys.ApiKeyError("回覆金鑰無效")
    monkeypatch.setattr(apikeys, "verify", fake)
    return calls


# ---------------------------------------------------------------------- 金鑰
def test_save_view_delete_key(server, verified):
    app = server[0]
    a, b = Client(app, "a"), Client(app, "b")
    r = a.req("PUT", "/api/keys/anthropic", json={"key": KEY_A})
    assert r.status_code == 200 and r.json()["last4"] == "WXYZ" and r.json()["set"] is True
    assert verified == [("anthropic", KEY_A, "")]
    view = a.req("GET", "/api/keys").json()
    assert view["allowed"] is True and view["keys"]["anthropic"]["last4"] == "WXYZ"
    assert KEY_A not in str(view) and not view["keys"]["openai"]["set"]
    assert b.req("GET", "/api/keys").json()["keys"]["anthropic"]["set"] is False        # 別人看不到
    row = app.state.db.get_api_keys(_uid(a))["anthropic"]
    assert KEY_A not in row["enc"] and "A" * 20 not in row["enc"]                         # 資料庫裡是加密的
    from app.web import apikeys
    assert apikeys.decrypt(server[1], row["enc"]) == KEY_A
    assert a.req("DELETE", "/api/keys/anthropic").status_code == 200
    assert a.req("GET", "/api/keys").json()["keys"]["anthropic"]["set"] is False
    assert a.req("DELETE", "/api/keys/evil").status_code == 404


@pytest.mark.parametrize("provider,body,word", [
    ("anthropic", {"key": "short"}, "格式不對"),
    ("anthropic", {"key": KEY_A + " x"}, "格式不對"),
    ("anthropic", {"key": "sk-ant-BAD-" + "x" * 30}, "無效"),
    ("anthropic", {}, "請貼上金鑰"),
    ("openai", {"key": KEY_O}, "模型名稱"),
    ("openai", {"key": KEY_O, "model": "gpt 4o; rm"}, "模型名稱只能"),
    ("gemini", {"key": KEY_O}, "不支援"),
])
def test_key_validation(server, verified, provider, body, word):
    a = Client(server[0], "a")
    r = a.req("PUT", f"/api/keys/{provider}", json=body)
    assert r.status_code == 400 and word in r.json()["error"], r.text
    assert not any(k["set"] for k in a.req("GET", "/api/keys").json()["keys"].values())


def test_openai_model_only_update_and_permissions(server, verified):
    app = server[0]
    a = Client(app, "a")
    assert a.req("PUT", "/api/keys/openai", json={"key": KEY_O, "model": "gpt-4o-mini"}).status_code == 200
    r = a.req("PUT", "/api/keys/openai", json={"key": None, "model": "gpt-4.1-mini"})     # 只改模型
    assert r.status_code == 200 and r.json()["model"] == "gpt-4.1-mini" and r.json()["last4"] == "9876"
    assert verified[-1] == ("openai", KEY_O, "gpt-4.1-mini")
    app.state.db.update_user(_uid(a), overrides={"api_translate": False})
    assert a.req("GET", "/api/keys").json()["allowed"] is False
    assert a.req("PUT", "/api/keys/anthropic", json={"key": KEY_A}).status_code == 403
    # 刪除我的資料:金鑰一起刪
    assert a.req("DELETE", "/api/my-data").status_code == 200
    assert app.state.db.get_api_keys(_uid(a)) == {}


def test_wrong_master_key_cannot_decrypt(server):
    from dataclasses import replace
    from app.web import apikeys
    cfg = server[1]
    token = apikeys.encrypt(cfg, KEY_A)
    assert apikeys.decrypt(replace(cfg, key_secret="other"), token) is None
    assert apikeys.decrypt(cfg, "garbage") is None


def test_production_requires_key_secret(env):
    from app.web import config as config_mod
    env.setenv("WEB_DEV_LOGIN", "0")
    env.setenv("WEB_SECRET_KEY", "s" * 40)
    env.delenv("WEB_KEY_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="WEB_KEY_SECRET"):
        config_mod.load()


@pytest.mark.parametrize("code,word", [(401, "無效"), (403, "無效"), (404, "找不到模型"), (500, "狀態碼 500"),
                                       (429, None), (200, None)])
def test_verify_responses(monkeypatch, code, word):
    from app.web import apikeys
    seen = []
    monkeypatch.setattr(apikeys, "_get", lambda url, headers: seen.append((url, headers)) or code)
    if word:
        with pytest.raises(apikeys.ApiKeyError, match=word):
            apikeys.verify("openai", KEY_O, "gpt-4o-mini")
    else:
        apikeys.verify("openai", KEY_O, "gpt-4o-mini")
    url, headers = seen[0]
    assert url == "https://api.openai.com/v1/models/gpt-4o-mini" and headers["Authorization"] == f"Bearer {KEY_O}"


def test_verify_network_error(monkeypatch):
    from app.web import apikeys

    def boom(url, headers):
        raise urllib.error.URLError("no route")
    monkeypatch.setattr(apikeys, "_get", boom)
    with pytest.raises(apikeys.ApiKeyError, match="暫時無法連到"):
        apikeys.verify("anthropic", KEY_A)


def test_job_gets_key_only_in_worker(server, verified, media_files):
    app = server[0]
    a = Client(app, "a")
    it = a.upload(media_files["a"]).json()
    r = _job(a, [it["id"]], translate_to="en", translation_engine="openai")
    assert r.status_code == 400 and "OpenAI API" in r.json()["error"]                 # 還沒填金鑰
    a.req("PUT", "/api/keys/openai", json={"key": KEY_O, "model": "gpt-4o-mini"})
    j = _job(a, [it["id"]], translate_to="en", translation_engine="openai").json()["job"]
    a.wait_job(j["id"])
    logs = " ".join(e["data"].get("text", "") for e in app.state.db.events_after(_uid(a), j["id"], 0))
    assert "api-key=set" in logs
    assert "api-client model=gpt-4o-mini from-env=True host=api.openai.com" in logs   # 核心讀到的是這個人的金鑰
    db = app.state.db
    with db.conn() as c:                                                              # 金鑰不在工作紀錄與事件裡
        dump = " ".join(str(tuple(r)) for t in ("jobs", "events") for r in c.execute(f"SELECT * FROM {t}"))
    assert KEY_O not in dump and "9876" not in dump
    assert db.get_job(_uid(a), j["id"])["settings"]["api"] == {"anthropic_model": "", "openai_base_url": "",
                                                                "openai_model": "gpt-4o-mini"}


def test_key_deleted_while_queued(quiet, verified, media_files):
    from app.web.runner import Runner
    db, cfg = quiet.state.db, quiet.state.cfg
    a = Client(quiet, "a")
    it = a.upload(media_files["a"]).json()
    a.req("PUT", "/api/keys/anthropic", json={"key": KEY_A})
    j = _job(a, [it["id"]], translate_to="en", translation_engine="anthropic").json()["job"]
    a.req("DELETE", "/api/keys/anthropic")
    Runner(cfg, db).run_job(db.claim_next_job())
    job = db.get_job(_uid(a), j["id"])
    assert job["status"] == "failed" and "設定" in job["error"]
    assert db.get_item(_uid(a), it["id"])["state"] == "failed"


def test_desktop_hints_rewritten():
    from app.translate.api import _fatal_reason
    from app.web.runner import web_hint

    class E(Exception):
        status_code = 401
    t = web_hint(_fatal_reason(E()))
    assert "set-api-key.bat" not in t and "請到「設定」頁重新填寫金鑰" in t
    E.status_code = 404
    assert web_hint(_fatal_reason(E())).endswith("請到「設定」頁確認模型名稱")
    assert "set-api-key" not in web_hint("尚未設定 anthropic 的 API 金鑰,請先執行 set-api-key.bat")


# ---------------------------------------------------------------------- 到期清除
def _done_item(c, path):
    it = c.upload(path).json()
    c.wait_job(_job(c, [it["id"]]).json()["job"]["id"])
    return it


def test_audio_then_subtitles_expire(server, media_files):
    from app.web import cleanup, storage
    app, cfg = server
    db = app.state.db
    a = Client(app, "a")
    it = _done_item(a, media_files["a"])
    st = a.req("GET", "/api/state").json()["items"][0]
    created = db.get_item(_uid(a), it["id"])["created_at"]
    assert abs(st["expires"]["audio"] - (created + 3 * DAY)) < 1 and st["expires"]["srt"] > created + 7 * DAY - 5
    s = cleanup.sweep(cfg, db, now=time.time() + 2 * DAY)
    assert s["items_deleted"] == s["audio_deleted"] == 0
    s = cleanup.sweep(cfg, db, now=time.time() + 4 * DAY)                    # 音訊到期,字幕還在
    assert s["audio_deleted"] == 1 and s["bytes_freed"] > 0
    row = db.get_item(_uid(a), it["id"])
    assert not storage.stored_path(cfg, row).is_file() and "mp3" not in row["outputs"]
    view = a.req("GET", "/api/state").json()["items"][0]
    assert view["audio_expired"] is True and view["outputs"] == ["srt"] and view["expires"]["audio"] is None
    r = _job(a, [it["id"]], mode="retranscribe")
    assert r.status_code == 400 and "音訊已到期刪除" in r.json()["error"]
    j = _job(a, [it["id"]], translate_to="en")                              # 只翻譯照樣可以
    assert j.status_code == 200, j.text
    a.wait_job(j.json()["job"]["id"])
    assert cleanup.sweep(cfg, db, now=time.time() + 6 * DAY)["items_deleted"] == 0   # 翻譯後重新計時
    assert cleanup.sweep(cfg, db, now=time.time() + 8 * DAY)["items_deleted"] == 1
    assert db.get_item(_uid(a), it["id"]) is None
    assert not storage.item_dir(cfg, row).exists()


def test_unprocessed_and_overrides(server, media_files):
    from app.web import cleanup
    app, cfg = server
    db = app.state.db
    a, b = Client(app, "a"), Client(app, "b")
    ia = a.upload(media_files["a"]).json()                                   # 沒處理過:照音訊天數整筆刪
    ib = b.upload(media_files["a"]).json()
    db.update_user(_uid(b), overrides={"keep_audio_days": 10})               # 個別加長
    s = cleanup.sweep(cfg, db, now=time.time() + 4 * DAY)
    assert s["items_deleted"] == 1 and db.get_item(_uid(a), ia["id"]) is None
    assert db.get_item(_uid(b), ib["id"]) is not None
    assert cleanup.sweep(cfg, db, now=time.time() + 11 * DAY)["items_deleted"] == 1


def test_busy_items_are_kept(quiet, media_files):
    from app.web import cleanup
    db, cfg = quiet.state.db, quiet.state.cfg
    a = Client(quiet, "a")
    it = a.upload(media_files["a"]).json()
    assert _job(a, [it["id"]]).status_code == 200                          # 排隊中(沒有執行器)
    assert cleanup.sweep(cfg, db, now=time.time() + 30 * DAY)["items_deleted"] == 0
    assert db.get_item(_uid(a), it["id"]) is not None


def test_choose_track_and_abandoned_uploads(server, more_media, media_files):
    from app.web import cleanup, uploads
    app, cfg = server
    db = app.state.db
    a = Client(app, "a")
    _, fin = chunked(a, more_media["multi"], content="original")
    assert fin.json()["state"] == "choose_track"
    r, _ = chunked(a, more_media["long"], stop_after=1)                    # 傳了一塊就停
    up = db.get_upload(_uid(a), r.json()["id"])
    assert uploads.upload_part(cfg, up).is_file()
    s = cleanup.sweep(cfg, db, now=time.time() + 0.5 * DAY)
    assert s["items_deleted"] == 0 and s["uploads_deleted"] == 0
    s = cleanup.sweep(cfg, db, now=time.time() + 1.1 * DAY)
    assert s["items_deleted"] == 1 and s["uploads_deleted"] == 1
    assert not uploads.upload_part(cfg, up).exists() and db.get_upload(_uid(a), up["id"]) is None


def test_admin_cleanup_endpoint_and_day_seconds(env, media_files):
    env.setenv("WEB_DAY_SECONDS", "10")
    from app.web import config as config_mod
    from app.web.api import create_app
    app = create_app(config_mod.load(), with_runner=False)
    with TestClient(app):
        boss, a = Client(app, "boss", admin=True), Client(app, "a")
        it = a.upload(media_files["a"]).json()
        assert a.req("POST", "/api/admin/cleanup").status_code == 403
        assert boss.req("GET", "/api/admin/users").json()["day_seconds"] == 10
        assert boss.req("POST", "/api/admin/cleanup").json()["items_deleted"] == 0
        app.state.db.update_user(_uid(a), overrides={"keep_audio_days": 1})
        time.sleep(10.5)                                                     # 驗收模式:1 天 = 10 秒
        r = boss.req("POST", "/api/admin/cleanup").json()
        assert r["items_deleted"] == 1, r
        assert a.req("GET", "/api/state").json()["items"] == []
        assert it["id"]


# ---------------------------------------------------------------------- 上傳方式
def test_upload_mode_pref(server):
    a = Client(server[0], "a")
    assert a.req("PUT", "/api/prefs", json={"upload_mode": "original"}).json()["upload_mode"] == "original"
    assert a.req("PUT", "/api/prefs", json={"upload_mode": "browser"}).status_code == 200
    r = a.req("PUT", "/api/prefs", json={"upload_mode": "weird"})
    assert r.status_code == 400
