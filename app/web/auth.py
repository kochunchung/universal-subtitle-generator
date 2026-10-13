"""登入狀態:伺服器簽署的 Cookie + CSRF 權杖。

第 1~2 階段只有「開發登入」(WEB_DEV_LOGIN=1 時才存在);第 3 階段加入 Google / LINE 等 OAuth 登入。
不論用哪種方式登入,後面的 API 都只認這裡的 current_user,所以換登入方式不影響其他程式。

- Cookie:HttpOnly、SameSite=Lax,正式環境(HTTPS)再加 Secure。
- 所有會改資料的請求(POST / PUT / DELETE)都要帶 X-CSRF-Token,值來自 /api/me。
"""
from __future__ import annotations

import hmac
import secrets
import time

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.web.config import Config
from app.web.db import DB

COOKIE = "session"
MAX_AGE = 14 * 24 * 3600
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _ser(cfg: Config) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(cfg.secret_key, salt="web-session")


def make_cookie(cfg: Config, user_id: int) -> str:
    return _ser(cfg).dumps({"uid": user_id, "csrf": secrets.token_urlsafe(24)})


def read_cookie(cfg: Config, value: str | None) -> dict | None:
    if not value:
        return None
    try:
        data = _ser(cfg).loads(value, max_age=MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return data if isinstance(data, dict) and "uid" in data and "csrf" in data else None


def session_of(request: Request) -> dict | None:
    cfg: Config = request.app.state.cfg
    return read_cookie(cfg, request.cookies.get(COOKIE))


def current_user(request: Request) -> dict:
    """已登入且已核准的使用者;否則 401 / 403。所有使用者資料的 API 都依賴它。"""
    cfg: Config = request.app.state.cfg
    db: DB = request.app.state.db
    sess = session_of(request)
    if not sess:
        raise HTTPException(401, "請先登入")
    user = db.get_user(int(sess["uid"]))
    if not user:
        raise HTTPException(401, "請重新登入")
    if user["status"] == "blocked":
        raise HTTPException(403, "這個帳號已被停用")
    if user["status"] != "approved":
        raise HTTPException(403, "帳號尚待管理者核准")
    if request.method not in SAFE_METHODS:
        token = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(token, str(sess["csrf"])):
            raise HTTPException(403, "請求驗證失敗,請重新整理頁面")
    if (user.get("last_seen") or 0) < time.time() - 300:     # 管理頁的「最後使用時間」
        db.touch_seen(user["id"])
    user = dict(user)
    user["csrf"] = sess["csrf"]
    return user


def admin_user(request: Request) -> dict:
    """管理者才能用的 API。第一位管理者由 .env 的 WEB_ADMIN_EMAILS 指定(登入時自動設定),
    之後可以在管理頁指定其他人。"""
    user = current_user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "只有管理者可以使用")
    return user


# ---------------------------------------------------------------------- Google 登入(OpenID Connect)
GOOGLE_METADATA = "https://accounts.google.com/.well-known/openid-configuration"
LOGIN_ERRORS = {
    "denied": "你取消了 Google 登入。",
    "unverified": "這個 Google 帳號的信箱還沒有驗證,無法登入。",
    "domain": "這個信箱不在允許登入的名單內。",
    "blocked": "這個帳號已被停用,請聯絡管理者。",
    "failed": "Google 登入失敗,請再試一次。",
    "disabled": "這個網站還沒有設定 Google 登入。",
}


def make_oauth(cfg: Config):
    """建立 Authlib 的 Google 用戶端;沒有設定 Client ID 時回傳 None。
    Authlib 會用 Google 公布的金鑰驗證 ID token 的簽章、發行者、對象、到期時間與 nonce。"""
    if not cfg.google_enabled:
        return None
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    oauth.register(
        "google",
        client_id=cfg.google_client_id,
        client_secret=cfg.google_client_secret,
        server_metadata_url=GOOGLE_METADATA,
        client_kwargs={"scope": "openid email profile"},
    )
    return oauth


def email_allowed(cfg: Config, email: str, hd: str | None) -> bool:
    """沒有設定網域限制時一律允許。有設定時:信箱網域、Google Workspace 網域(hd)或個別信箱任一符合即可。"""
    if not cfg.allowed_domains and not cfg.allowed_emails:
        return True
    email = (email or "").lower()
    domain = email.rpartition("@")[2]
    return (email in cfg.allowed_emails or domain in cfg.allowed_domains
            or (hd or "").lower() in cfg.allowed_domains)


def google_user(cfg: Config, db: DB, info: dict) -> tuple[dict | None, str]:
    """依 Google 回傳的使用者資料建立或更新帳號。回傳(使用者, 錯誤代碼);錯誤代碼見 LOGIN_ERRORS。"""
    sub = str(info.get("sub") or "")
    email = str(info.get("email") or "")
    if not sub:
        return None, "failed"
    if not info.get("email_verified"):
        return None, "unverified"
    if not email_allowed(cfg, email, info.get("hd")):
        return None, "domain"
    name = str(info.get("name") or email.partition("@")[0])[:80]
    is_admin = email.lower() in cfg.admin_emails
    status = "pending" if cfg.require_approval and not is_admin else "approved"
    user = db.upsert_user("google", sub, email, name, status=status, is_admin=is_admin)
    if is_admin and not user["is_admin"]:
        db.update_user(user["id"], is_admin=1)
    if is_admin and user["status"] == "pending":         # 管理者不用等核准;但被停用的帳號仍然停用
        db.update_user(user["id"], status="approved")
    user = db.get_user(user["id"])
    if user["status"] == "blocked":
        return None, "blocked"
    return user, ""
