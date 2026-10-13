"""第 4 階段:分塊上傳、伺服器抽音訊(備援)、多音軌選擇、各項上限與每日額度。"""
import subprocess
import time
from pathlib import Path

import pytest

from tests.web.test_web import _job

MB = 2**20


def _ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def more_media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media4")
    out = {}
    out["long"] = d / "long.mp3"
    _ff("-f", "lavfi", "-i", "sine=frequency=300:duration=70", "-ac", "1", "-b:a", "128k", str(out["long"]))
    out["video"] = d / "會議.mp4"
    _ff("-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-f", "lavfi", "-i", "sine=frequency=330",
        "-t", "3", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(out["video"]))
    out["multi"] = d / "two tracks.mkv"           # 第 1 軌 2 秒、第 2 軌 4 秒,才分得出抽的是哪一軌
    _ff("-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=300:duration=2", "-f", "lavfi", "-i", "sine=frequency=600:duration=4",
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
        str(out["multi"]))
    out["audio_of_video"] = d / "extracted.mp3"   # 模擬瀏覽器抽好的音訊
    _ff("-i", str(out["video"]), "-vn", "-ac", "1", "-codec:a", "libmp3lame", "-b:a", "128k", str(out["audio_of_video"]))
    return out


def chunked(c, path: Path, name=None, content="audio", stop_after=None):
    """照網頁介面的方式分塊上傳;回傳 (start 回應, finish 回應或 None)。"""
    data = path.read_bytes()
    r = c.req("POST", "/api/uploads", json={"name": name or path.name, "size": len(data), "content": content})
    if r.status_code != 200:
        return r, None
    up = r.json()
    off, n = 0, 0
    while off < len(data):
        if stop_after is not None and n >= stop_after:
            return r, None
        part = data[off:off + up["chunk_bytes"]]
        rr = c.req("PUT", f"/api/uploads/{up['id']}", params={"offset": off}, content=part)
        assert rr.status_code == 200, rr.text
        off = rr.json()["received"]
        n += 1
    return r, c.req("POST", f"/api/uploads/{up['id']}/finish")


def wait_state(c, item_id, states, timeout=30):
    t = time.time()
    while time.time() - t < timeout:
        it = next(i for i in c.req("GET", "/api/state").json()["items"] if i["id"] == item_id)
        if it["state"] in states:
            return it
        time.sleep(0.1)
    raise AssertionError(f"狀態逾時:{it}")


@pytest.fixture()
def small_chunks(env):
    """conftest 已把分塊設成 1 MB;保留這個名稱讓測試意圖清楚。"""


def test_browser_audio_keeps_video_name(alice, more_media, server, small_chunks):
    cfg = server[1]
    _, fin = chunked(alice, more_media["audio_of_video"], name="會議.mp4", content="audio")
    assert fin.status_code == 200, fin.text
    it = fin.json()
    assert it["name"] == "會議.mp4" and it["kind"] == "media" and it["state"] == "uploaded"
    files = [p.name for p in cfg.item_dir(alice.req("GET", "/api/me").json()["id"], it["id"]).iterdir()]
    assert files == [f"{it['id']}.mp3"]                  # 伺服器只有音訊,沒有影片
    alice.wait_job(_job(alice, [it["id"]]).json()["job"]["id"])
    d = alice.req("GET", f"/api/items/{it['id']}/download", params={"which": "srt"})
    assert "%E6%9C%83%E8%AD%B0.zh-TW.srt" in d.headers["content-disposition"]   # 會議.zh-TW.srt
    assert alice.req("GET", "/api/uploads").json() == []


def test_resume_after_interruption(alice, more_media, small_chunks):
    big = more_media["long"]                              # 約 1.1 MB → 2 塊
    data = big.read_bytes()
    up = alice.req("POST", "/api/uploads", json={"name": "long.mp3", "size": len(data), "content": "audio"}).json()
    r = alice.req("PUT", f"/api/uploads/{up['id']}", params={"offset": 0}, content=data[:MB])
    assert r.json()["received"] == MB
    # 斷線後重送同一塊:伺服器回 409 與實際收到的位置
    r = alice.req("PUT", f"/api/uploads/{up['id']}", params={"offset": 0}, content=data[:MB])
    assert r.status_code == 409 and r.json()["received"] == MB
    assert alice.req("POST", f"/api/uploads/{up['id']}/finish").status_code == 409     # 還沒傳完
    # 重新整理頁面後,清單裡找得到這次上傳,從 1 MB 接著傳
    pending = alice.req("GET", "/api/uploads").json()
    assert pending == [{"id": up["id"], "name": "long.mp3", "size": len(data), "content": "audio", "received": MB}]
    r = alice.req("PUT", f"/api/uploads/{up['id']}", params={"offset": MB}, content=data[MB:])
    assert r.json()["received"] == len(data)
    fin = alice.req("POST", f"/api/uploads/{up['id']}/finish")
    assert fin.status_code == 200 and fin.json()["duration"] > 60


def test_cancel_upload(alice, more_media, small_chunks, server):
    cfg = server[1]
    r, _ = chunked(alice, more_media["long"], stop_after=1)
    uid = r.json()["id"]
    assert alice.req("DELETE", f"/api/uploads/{uid}").json() == {"ok": True}
    assert alice.req("GET", "/api/uploads").json() == []
    assert not list(cfg.data_dir.rglob(f"{uid}.part"))


def test_original_fallback_extracts_and_deletes_video(alice, more_media, server, small_chunks):
    cfg = server[1]
    _, fin = chunked(alice, more_media["video"], content="original")
    it = fin.json()
    assert it["state"] in ("extracting", "uploaded")
    it = wait_state(alice, it["id"], ("uploaded", "failed"))
    assert it["state"] == "uploaded", it
    d = cfg.item_dir(alice.req("GET", "/api/me").json()["id"], it["id"])
    assert [p.name for p in d.iterdir()] == [f"{it['id']}.mp3"]        # 抽完立即刪除影片
    assert alice.wait_job(_job(alice, [it["id"]]).json()["job"]["id"])["status"] == "done"


def test_multi_track_choose(alice, bob, more_media, server, small_chunks):
    from app.core import media
    cfg = server[1]
    _, fin = chunked(alice, more_media["multi"], content="original")
    it = fin.json()
    assert it["state"] == "choose_track" and len(it["audio_streams"]) == 2
    r = _job(alice, [it["id"]])                                          # 還沒選音軌:不能處理
    assert r.status_code == 400 and "音軌" in r.json()["error"]
    assert bob.req("POST", f"/api/items/{it['id']}/track", json={"stream": 1}).status_code == 404
    assert alice.req("POST", f"/api/items/{it['id']}/track", json={"stream": 5}).status_code == 400
    assert alice.req("POST", f"/api/items/{it['id']}/track", json={"stream": 1}).status_code == 200
    it = wait_state(alice, it["id"], ("uploaded", "failed"))
    assert it["state"] == "uploaded"
    mp3 = cfg.item_dir(alice.req("GET", "/api/me").json()["id"], it["id"]) / f"{it['id']}.mp3"
    assert 3.5 < media.probe(mp3).duration < 4.5                        # 抽的是 4 秒的第 2 軌
    assert alice.req("POST", f"/api/items/{it['id']}/track", json={"stream": 0}).status_code == 409


def test_bad_original_reports_failure(alice, media_files, small_chunks):
    r, fin = chunked(alice, media_files["bad"], content="original")
    assert fin.status_code == 400 and "/" not in fin.json()["error"].replace("檔案", "")
    assert alice.req("GET", "/api/state").json()["items"] == []



def limited(app, name, tier="paid", **overrides):
    """建立一個帳號,並用個別設定把上限調小(第 4.5 階段起上限來自會員等級與個別設定)。"""
    from tests.web.conftest import Client
    c = Client(app, name, tier=tier)
    if overrides:
        uid = c.req("GET", "/api/me").json()["id"]
        app.state.db.update_user(uid, overrides=overrides)
    return c


@pytest.mark.parametrize("extra,name,size,content,status,word", [
    ({"audio_mb": 1}, "a.mp4", 2 * MB, "audio", 413, "音訊超過 1 MB"),
    ({"audio_mb": 1}, "a.wav", 2 * MB, "original", 413, "音訊超過 1 MB"),
    ({"original_mb": 2}, "a.mp4", 3 * MB, "original", 413, "原始檔案超過 2 MB"),
    ({}, "a.srt", 9 * MB, "srt", 413, "字幕檔超過"),
    ({}, "a.exe", 10, "original", 415, "不支援"),
    ({}, "a.mp4", 10, "weird", 400, "參數"),
])
def test_declared_size_limits(server, extra, name, size, content, status, word):
    c = limited(server[0], "zed", **extra)
    r = c.req("POST", "/api/uploads", json={"name": name, "size": size, "content": content})
    assert r.status_code == status and word in r.json()["error"], r.text


def test_storage_quota_counts_pending_uploads(server, more_media):
    c = limited(server[0], "q", storage_mb=2)
    r1 = c.req("POST", "/api/uploads", json={"name": "x.mp3", "size": int(1.5 * MB), "content": "audio"})
    assert r1.status_code == 200
    r2 = c.req("POST", "/api/uploads", json={"name": "y.mp3", "size": int(0.8 * MB), "content": "audio"})
    assert r2.status_code == 413 and "空間不足" in r2.json()["error"]       # 還沒傳完的也先預留
    c.req("DELETE", f"/api/uploads/{r1.json()['id']}")
    assert c.req("POST", "/api/uploads", json={"name": "y.mp3", "size": int(0.8 * MB), "content": "audio"}).status_code == 200
    u = c.req("GET", "/api/usage").json()
    assert u["storage_limit"] == 2 * MB and u["limits"]["audio_bytes"] == 300 * MB


def test_duration_limit(server, more_media):
    c = limited(server[0], "d", duration_min=1)
    r = c.upload(more_media["long"])                                     # 70 秒 > 1 分鐘
    assert r.status_code == 413 and "長度" in r.json()["error"]
    assert c.req("GET", "/api/state").json()["items"] == []
    assert c.req("GET", "/api/usage").json()["storage_bytes"] == 0      # 被拒的檔案不留在伺服器


def test_daily_quota(server, more_media):
    c = limited(server[0], "daily", daily_minutes=2)
    a = c.upload(more_media["long"]).json()                              # 70 秒
    b = c.upload(more_media["long"], name="long2.mp3").json()            # 70 秒
    j1 = _job(c, [a["id"]])
    assert j1.status_code == 200
    c.wait_job(j1.json()["job"]["id"])
    r = _job(c, [b["id"]])                                               # 70 + 70 > 120 秒
    assert r.status_code == 400 and "今天的處理額度剩 1 分鐘" in r.json()["error"]
    # 只翻譯不算額度
    r = _job(c, [a["id"]], translate_to="en")
    assert r.status_code == 200, r.text
    c.wait_job(r.json()["job"]["id"])
    u = c.req("GET", "/api/usage").json()
    assert 69 < u["today_seconds"] < 71 and u["daily_limit"] == 120


def test_upload_isolation(alice, bob, more_media, small_chunks):
    r, _ = chunked(alice, more_media["long"], stop_after=1)
    uid = r.json()["id"]
    assert bob.req("GET", f"/api/uploads/{uid}").status_code == 404
    assert bob.req("PUT", f"/api/uploads/{uid}", params={"offset": MB}, content=b"x").status_code == 404
    assert bob.req("POST", f"/api/uploads/{uid}/finish").status_code == 404
    assert bob.req("DELETE", f"/api/uploads/{uid}").status_code == 404
    assert bob.req("GET", "/api/uploads").json() == []


def test_extractor_recover(server, alice, more_media, small_chunks):
    app, cfg = server
    db = app.state.db
    _, fin = chunked(alice, more_media["multi"], content="original")
    it = fin.json()
    db.update_item(it["id"], state="extracting", stream=0)              # 模擬伺服器在抽音訊途中重開
    app.state.extractor.recover()
    it = wait_state(alice, it["id"], ("uploaded", "failed"))
    assert it["state"] == "uploaded"
