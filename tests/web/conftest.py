import os
import subprocess
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.web import config as config_mod
from app.web.api import create_app


@pytest.fixture(scope="session")
def media_files(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    out = {}
    for name, secs in (("a", 2), ("b", 3)):
        p = d / f"{name}.mp3"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"sine=frequency=440:duration={secs}", "-ac", "1", str(p)], check=True)
        out[name] = p
    v = d / "會議 記錄.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10",
                    "-f", "lavfi", "-i", "sine=frequency=330", "-t", "2", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", str(v)], check=True)
    out["video"] = v
    bad = d / "bad.mp4"
    bad.write_bytes(os.urandom(5000))
    out["bad"] = bad
    return out


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WEB_DEV_LOGIN", "1")
    monkeypatch.setenv("WEB_WORKER_TARGET", "tests.web.fake_worker:run")
    monkeypatch.setenv("WEB_ENGINES", "m2m100,anthropic,openai")
    monkeypatch.setenv("WEB_CHUNK_MB", "1")           # 測試檔案小,分塊也要小,才測得到續傳
    return monkeypatch


class Client:
    """帶登入與 CSRF 的測試用戶端。"""

    def __init__(self, app, name="alice", tier="paid", admin=False):
        self.c = TestClient(app, base_url="http://testserver")
        self.c.post("/api/dev-login", json={"name": name, "tier": tier, "admin": admin})
        self.csrf = self.c.get("/api/me").json()["csrf"]

    def req(self, method, url, **kw):
        h = kw.pop("headers", {})
        h["X-CSRF-Token"] = self.csrf
        return self.c.request(method, url, headers=h, **kw)

    def upload(self, path: Path, name=None):
        with open(path, "rb") as f:
            return self.req("PUT", "/api/items", params={"name": name or path.name}, content=f.read())

    def wait_job(self, job_id, want=("done", "failed", "canceled"), timeout=30):
        t = time.time()
        while time.time() - t < timeout:
            j = self.req("GET", f"/api/jobs/{job_id}").json()
            if j["status"] in want:
                return j
            time.sleep(0.1)
        raise AssertionError("工作逾時:" + str(j))


@pytest.fixture()
def server(env):
    cfg = config_mod.load()
    app = create_app(cfg, with_runner=True)
    with TestClient(app):          # 觸發 lifespan,啟動執行器執行緒
        yield app, cfg


@pytest.fixture()
def alice(server):
    return Client(server[0], "alice")


@pytest.fixture()
def bob(server):
    return Client(server[0], "bob")
