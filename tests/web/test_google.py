"""第 3 階段:Google 登入(用假的 Google 回應測試,不連網)。"""
import pytest
from starlette.responses import RedirectResponse
from starlette.testclient import TestClient

from app.web import config as config_mod
from app.web.api import create_app

BASE = "http://localhost:8000"


def _info(sub="g-1", email="amy@gmail.com", verified=True, name="Amy", hd=None):
    d = {"sub": sub, "email": email, "email_verified": verified, "name": name}
    if hd:
        d["hd"] = hd
    return d


@pytest.fixture()
def gapp(env):
    env.setenv("WEB_GOOGLE_CLIENT_ID", "test-id.apps.googleusercontent.com")
    env.setenv("WEB_GOOGLE_CLIENT_SECRET", "test-secret")
    env.setenv("WEB_PUBLIC_URL", BASE)
    env.setenv("WEB_DEV_LOGIN", "0")
    env.setenv("WEB_SECRET_KEY", "k" * 40)
    env.setenv("WEB_KEY_SECRET", "m" * 40)
    env.setenv("WEB_REQUIRE_APPROVAL", "0")       # 這組測試只測登入流程;審核另外測

    def make(**extra):
        for k, v in extra.items():
            env.setenv(k, v)
        app = create_app(config_mod.load(), with_runner=False)
        calls = {}
        google = app.state.oauth.google

        async def fake_redirect(request, redirect_uri, **kw):
            calls["redirect_uri"] = redirect_uri
            calls["kw"] = kw
            return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?fake=1", status_code=302)

        async def fake_token(request):
            if isinstance(calls.get("next"), Exception):
                raise calls["next"]
            return {"userinfo": calls["next"]}

        google.authorize_redirect = fake_redirect
        google.authorize_access_token = fake_token
        return app, calls
    return make


def _login(app, calls, info, base=BASE):
    c = TestClient(app, base_url=base)
    calls["next"] = info
    r = c.get("/auth/google/callback", params={"code": "x", "state": "y"}, follow_redirects=False)
    return c, r


def test_public_info_and_dev_login_off(gapp):
    app, _ = gapp()
    c = TestClient(app, base_url=BASE)
    assert c.get("/api/public-info").json() == {"dev_login": False, "google": True}
    assert c.post("/api/dev-login", json={"name": "x"}).status_code == 404     # 正式設定下沒有開發登入


def test_login_redirect_uses_public_url(gapp):
    app, calls = gapp()
    r = TestClient(app, base_url=BASE).get("/auth/google/login", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://accounts.google.com/")
    assert calls["redirect_uri"] == BASE + "/auth/google/callback"
    assert calls["kw"] == {"prompt": "select_account"}
    # 用 127.0.0.1 打開:先轉到設定的網址,Cookie 才不會分家
    r = TestClient(app, base_url="http://127.0.0.1:8000").get("/auth/google/login", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == BASE + "/auth/google/login"


def test_callback_creates_user_and_logs_in(gapp):
    app, calls = gapp()
    c, r = _login(app, calls, _info())
    assert r.status_code == 303 and r.headers["location"] == "/"
    me = c.get("/api/me").json()
    assert me["status"] == "approved" and me["name"] == "Amy" and me["is_admin"] is False
    assert c.get("/api/state").status_code == 200
    # 同一個 Google 帳號再登入:同一個使用者,名稱跟著更新
    c2, _ = _login(app, calls, _info(name="Amy Lin"))
    me2 = c2.get("/api/me").json()
    assert me2["id"] == me["id"] and me2["name"] == "Amy Lin"
    assert len(app.state.db.list_users()) == 1


@pytest.mark.parametrize("info,code", [
    (_info(verified=False), "unverified"),
    ({"email": "a@b.c", "email_verified": True}, "failed"),         # 沒有 sub
])
def test_callback_rejects(gapp, info, code):
    app, calls = gapp()
    c, r = _login(app, calls, info)
    assert r.status_code == 303 and r.headers["location"] == f"/?login_error={code}"
    assert c.get("/api/me").status_code == 401
    assert app.state.db.list_users() == []


def test_user_cancelled_and_oauth_error(gapp):
    from authlib.integrations.starlette_client import OAuthError
    app, calls = gapp()
    c = TestClient(app, base_url=BASE)
    r = c.get("/auth/google/callback", params={"error": "access_denied"}, follow_redirects=False)
    assert r.headers["location"] == "/?login_error=denied"
    calls["next"] = OAuthError(error="mismatching_state")
    r = c.get("/auth/google/callback", params={"code": "x", "state": "bad"}, follow_redirects=False)
    assert r.headers["location"] == "/?login_error=failed"


def test_domain_restriction(gapp):
    app, calls = gapp(WEB_ALLOWED_EMAIL_DOMAINS="example.com", WEB_ALLOWED_EMAILS="friend@gmail.com")
    _, r = _login(app, calls, _info(email="x@gmail.com"))
    assert r.headers["location"] == "/?login_error=domain"
    _, r = _login(app, calls, _info(sub="g-2", email="ch1@EXAMPLE.com"))
    assert r.headers["location"] == "/"
    _, r = _login(app, calls, _info(sub="g-3", email="a@other.com", hd="example.com"))
    assert r.headers["location"] == "/"
    _, r = _login(app, calls, _info(sub="g-4", email="friend@gmail.com"))
    assert r.headers["location"] == "/"


def test_blocked_user_cannot_log_in(gapp):
    app, calls = gapp()
    c, _ = _login(app, calls, _info())
    uid = c.get("/api/me").json()["id"]
    app.state.db.update_user(uid, status="blocked")
    assert c.get("/api/state").status_code == 403                     # 已登入的人也立即失效
    c2, r = _login(app, calls, _info())
    assert r.headers["location"] == "/?login_error=blocked"
    assert c2.get("/api/me").status_code == 401


def test_approval_switch_and_admin(gapp):
    app, calls = gapp(WEB_REQUIRE_APPROVAL="1", WEB_ADMIN_EMAILS="boss@example.com")
    c, _ = _login(app, calls, _info())
    assert c.get("/api/me").json()["status"] == "pending"
    assert c.get("/api/state").status_code == 403
    c2, _ = _login(app, calls, _info(sub="g-9", email="boss@example.com"))
    me = c2.get("/api/me").json()
    assert me["status"] == "approved" and me["is_admin"] is True


def test_two_google_users_isolated(gapp, media_files):
    app, calls = gapp()
    a, _ = _login(app, calls, _info(sub="A", email="a@gmail.com"))
    b, _ = _login(app, calls, _info(sub="B", email="b@gmail.com"))
    ha = {"X-CSRF-Token": a.get("/api/me").json()["csrf"]}
    hb = {"X-CSRF-Token": b.get("/api/me").json()["csrf"]}
    item = a.put("/api/items", params={"name": "a.mp3"}, content=media_files["a"].read_bytes(), headers=ha).json()
    assert b.get("/api/state").json()["items"] == []
    for url in (f"/api/items/{item['id']}/preview", f"/api/items/{item['id']}/download"):
        assert b.get(url).status_code == 404
    assert b.delete(f"/api/items/{item['id']}", headers=hb).status_code == 404
    # 沒有 CSRF 權杖的跨站請求被擋
    assert a.delete(f"/api/items/{item['id']}").status_code == 403
    a.post("/api/logout", headers=ha)
    assert a.get("/api/state").status_code == 401


def test_secure_cookies_on_https(gapp):
    app, calls = gapp(WEB_PUBLIC_URL="https://subs.example.com")
    c = TestClient(app, base_url="https://subs.example.com")
    calls["next"] = _info()
    r = c.get("/auth/google/callback", params={"code": "x"}, follow_redirects=False)
    cookie = r.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()


def test_real_authlib_flow_with_fake_google(env):
    """用真正的 Authlib 跑完整流程(state、nonce、ID token 簽章驗證),Google 換成本機假的伺服器。"""
    import json
    import time
    import urllib.parse

    from authlib.integrations.httpx_client._compat import httpx2 as httpx   # Authlib 實際用的 httpx(2)
    from authlib.jose import JsonWebKey, jwt
    from authlib.integrations.starlette_client import OAuth

    env.setenv("WEB_GOOGLE_CLIENT_ID", "cid")
    env.setenv("WEB_GOOGLE_CLIENT_SECRET", "secret")
    env.setenv("WEB_PUBLIC_URL", BASE)
    env.setenv("WEB_SECRET_KEY", "s" * 40)
    env.setenv("WEB_KEY_SECRET", "m" * 40)
    env.setenv("WEB_REQUIRE_APPROVAL", "0")
    app = create_app(config_mod.load(), with_runner=False)

    key = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "k1"})
    issuer = "https://accounts.google.com"
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/jwks":
            return httpx.Response(200, json={"keys": [key.as_dict(is_private=False)]})
        if req.url.path == "/token":
            form = dict(urllib.parse.parse_qsl(req.content.decode()))
            assert form["code"] == "good-code" and form["redirect_uri"] == BASE + "/auth/google/callback"
            now = int(time.time())
            claims = {"iss": issuer, "aud": "cid", "sub": "real-1", "email": "real@gmail.com",
                      "email_verified": True, "name": "Real", "iat": now, "exp": now + 600, "nonce": seen["nonce"]}
            idt = jwt.encode({"alg": "RS256", "kid": "k1"}, claims, key).decode()
            return httpx.Response(200, json={"access_token": "at", "token_type": "Bearer", "expires_in": 600,
                                             "id_token": idt, "scope": "openid email profile"})
        return httpx.Response(404)

    oauth = OAuth()
    oauth.register("google", client_id="cid", client_secret="secret",
                   authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
                   access_token_url="https://oauth2.googleapis.com/token",
                   jwks_uri="https://www.googleapis.com/jwks",
                   server_metadata={"issuer": issuer, "jwks_uri": "https://www.googleapis.com/jwks",
                                    "id_token_signing_alg_values_supported": ["RS256"]},
                   client_kwargs={"scope": "openid email profile", "transport": httpx.MockTransport(handler)})
    oauth.google.server_metadata.update({"issuer": issuer, "jwks_uri": "https://www.googleapis.com/jwks"})
    app.state.oauth = oauth

    c = TestClient(app, base_url=BASE)
    r = c.get("/auth/google/login", follow_redirects=False)
    loc = urllib.parse.urlparse(r.headers["location"])
    q = dict(urllib.parse.parse_qsl(loc.query))
    assert loc.netloc == "accounts.google.com" and q["redirect_uri"] == BASE + "/auth/google/callback"
    assert q["scope"] == "openid email profile" and q["state"] and q["nonce"]
    seen["nonce"] = q["nonce"]
    # 竄改過的 state 會被擋
    r = c.get("/auth/google/callback", params={"code": "good-code", "state": "forged"}, follow_redirects=False)
    assert r.headers["location"] == "/?login_error=failed"
    # 重新登入一次,用正確的 state
    r = c.get("/auth/google/login", follow_redirects=False)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(r.headers["location"]).query))
    seen["nonce"] = q["nonce"]
    r = c.get("/auth/google/callback", params={"code": "good-code", "state": q["state"]}, follow_redirects=False)
    assert r.headers["location"] == "/", r.headers["location"]
    me = c.get("/api/me").json()
    assert me["name"] == "Real" and me["status"] == "approved"


def test_setup_google_env_editing():
    from app.web.setup_google import current, set_values
    lines = ["# 註解", "WEB_PORT=8000", "WEB_GOOGLE_CLIENT_ID=old"]
    out = set_values(lines, {"WEB_GOOGLE_CLIENT_ID": "new", "WEB_SECRET_KEY": "k"})
    assert out == ["# 註解", "WEB_PORT=8000", "WEB_GOOGLE_CLIENT_ID=new", "WEB_SECRET_KEY=k"]
    assert current(out, "WEB_PORT") == "8000" and current(out, "NOPE") == ""
