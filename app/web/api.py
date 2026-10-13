"""網頁版後端 API(FastAPI)。

原則:
- 所有使用者資料的 API 都依賴 auth.current_user,查詢一律帶 user_id;別人的資料一律回「找不到」(404),
  不洩漏它是否存在。
- 沒有任何公開的檔案網址;下載每次都檢查擁有者。
- 錯誤訊息不帶伺服器路徑,日誌不記字幕內容與金鑰。
"""
from __future__ import annotations

import io
import json
import logging
import zipfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.web import apikeys, auth, cleanup, jobs, perms, preview, storage, uploads
from app.web import config as config_mod
from app.web.config import Config
from app.web.db import DB
from app.web.runner import Runner

log = logging.getLogger("web.api")
STATIC = Path(__file__).resolve().parent / "static"
MAX_GLOSSARY_BYTES = 64 * 1024
NO_STORE = {"Cache-Control": "no-store"}


class DevLogin(BaseModel):
    name: str
    admin: bool = False
    tier: str = "free"
    pending: bool = False             # 模擬剛用 Google 登入、還沒核准的新帳號


class Glossary(BaseModel):
    text: str


class UploadStart(BaseModel):
    name: str
    size: int
    content: str = "audio"


class TrackChoice(BaseModel):
    stream: int


class KeyInput(BaseModel):
    key: str | None = None            # 留空 = 只改模型名稱,金鑰沿用
    model: str | None = None


class UserUpdate(BaseModel):
    """管理頁對單一帳號的修改;沒給的欄位不動。overrides 的項目給 null = 改回照等級。"""
    status: str | None = None
    tier: str | None = None
    is_admin: bool | None = None
    overrides: dict | None = None


class JobRequest(BaseModel):
    item_ids: list[str]
    mode: str = "auto"
    options: dict = {}


def create_app(cfg: Config | None = None, with_runner: bool = False) -> FastAPI:
    cfg = cfg or config_mod.load()
    db = DB(cfg.db_path, cfg.priority_max_wait_s)
    extractor = uploads.Extractor(cfg, db)
    cleaner = cleanup.Cleaner(cfg, db)
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        thread = None
        extractor.recover()
        threading.Thread(target=cleaner.run_forever, args=(stop,), name="cleanup", daemon=True).start()
        if with_runner:
            runner = Runner(cfg, db)
            thread = threading.Thread(target=runner.run_forever, args=(stop,), name="gpu-runner", daemon=True)
            thread.start()
        yield
        stop.set()
        extractor.shutdown()
        if thread:
            thread.join(5)

    app = FastAPI(title="萬能字幕產生器(網頁版)", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg, app.state.db, app.state.extractor, app.state.cleaner = cfg, db, extractor, cleaner
    app.state.oauth = auth.make_oauth(cfg)
    # Google 登入時暫存 state / nonce 用的短期 Cookie(10 分鐘),和登入狀態的 Cookie 分開
    app.add_middleware(SessionMiddleware, secret_key=cfg.secret_key + ":oauth", session_cookie="oauth_state",
                       max_age=600, same_site="lax", https_only=cfg.secure_cookies)
    user_dep = Depends(auth.current_user)

    def set_login_cookie(response: Response, user_id: int) -> None:
        response.set_cookie(auth.COOKIE, auth.make_cookie(cfg, user_id), max_age=auth.MAX_AGE,
                            httponly=True, samesite="lax", secure=cfg.secure_cookies)

    # ------------------------------------------------------------------ 頁面
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "app.html", headers=NO_STORE)

    @app.get("/dev", include_in_schema=False)
    def dev_page():                      # 第 1 階段的開發頁已併入正式介面
        return RedirectResponse("/")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/vendor/{key}/{fname}", include_in_schema=False)
    def vendor_file(key: str, fname: str):
        """瀏覽器抽音訊元件(ffmpeg.wasm),第一次會從 CDN 下載並快取。"""
        from app.web import vendor

        if not vendor.allowed(key, fname):
            raise HTTPException(404, "not found")
        try:
            p = vendor.path(key, fname)
        except Exception as e:  # noqa: BLE001
            log.warning("抽音訊元件下載失敗:%s", e)
            raise HTTPException(502, "無法下載瀏覽器抽音訊元件") from None
        ctype = "application/wasm" if fname.endswith(".wasm") else "text/javascript; charset=utf-8"
        return FileResponse(p, media_type=ctype, headers={"Cache-Control": "public, max-age=604800"})

    # ------------------------------------------------------------------ 登入
    @app.post("/api/dev-login")
    def dev_login(body: DevLogin, response: Response):
        if not cfg.dev_login:
            raise HTTPException(404, "not found")
        name = storage.clean_name(body.name)[:40]
        if body.tier not in perms.TIERS:
            raise HTTPException(400, "不支援的等級")
        # 開發用:每次登入都套用畫面上選的等級、管理者與是否待核准,方便測試各種情況
        status = "pending" if body.pending and not body.admin else "approved"
        user = db.upsert_user("dev", name, None, name, status=status, tier=body.tier, is_admin=body.admin)
        db.update_user(user["id"], status=status, tier=body.tier, is_admin=int(body.admin))
        db.touch_login(user["id"])
        set_login_cookie(response, user["id"])
        return {"ok": True}

    # ------------------------------------------------------------------ Google 登入
    def login_error(code: str) -> RedirectResponse:
        return RedirectResponse(f"/?login_error={quote(code)}", status_code=303)

    @app.get("/auth/google/login", include_in_schema=False)
    async def google_login(request: Request):
        oauth = request.app.state.oauth
        if oauth is None:
            return login_error("disabled")
        # Cookie 綁網址:使用者用別的網址(例如 127.0.0.1)打開時,先轉到設定的網址再登入
        # (只比對主機名稱與埠號;放在反向代理後面時,程式看到的是 http,但網址是 https)
        base = cfg.public_url
        if request.url.netloc.lower() != base.split("://", 1)[1].lower():
            return RedirectResponse(base + "/auth/google/login", status_code=303)
        return await oauth.google.authorize_redirect(request, base + "/auth/google/callback",
                                                     prompt="select_account")

    @app.get("/auth/google/callback", include_in_schema=False)
    async def google_callback(request: Request):
        oauth = request.app.state.oauth
        if oauth is None:
            return login_error("disabled")
        if request.query_params.get("error"):
            return login_error("denied")
        from authlib.integrations.starlette_client import OAuthError

        try:
            token = await oauth.google.authorize_access_token(request)
        except OAuthError as e:
            log.warning("Google 登入失敗:%s", e.error)
            return login_error("failed")
        except Exception:  # noqa: BLE001  網路或憑證問題
            log.exception("Google 登入失敗")
            return login_error("failed")
        info = token.get("userinfo") or {}
        user, err = auth.google_user(cfg, db, info)
        if err:
            log.info("Google 登入被拒(%s)", err)
            return login_error(err)
        log.info("使用者 %s 以 Google 登入(%s)", user["id"], user["status"])
        db.touch_login(user["id"])
        resp = RedirectResponse("/", status_code=303)
        set_login_cookie(resp, user["id"])
        return resp

    @app.get("/api/public-info")
    def public_info():
        """登入頁需要知道的公開資訊(不含任何使用者資料)。"""
        return {"dev_login": cfg.dev_login, "google": cfg.google_enabled}

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        response.delete_cookie(auth.COOKIE)
        request.session.clear()
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request):
        sess = auth.session_of(request)
        user = db.get_user(int(sess["uid"])) if sess else None
        if not user:
            raise HTTPException(401, "請先登入")
        tier = user["tier"] if user["tier"] in perms.TIERS else "free"
        return {"id": user["id"], "name": user["name"], "email": user["email"], "status": user["status"],
                "tier": tier, "tier_label": perms.TIER_LABELS[tier],
                "is_admin": bool(user["is_admin"]), "csrf": sess["csrf"], "dev_login": cfg.dev_login}

    @app.get("/api/options")
    def options(user: dict = user_dep):
        return jobs.public_options(cfg, perms.effective(db, user))

    # ------------------------------------------------------------------ 項目(檔案)
    @app.get("/api/state")
    def state(user: dict = user_dep):
        """重新整理頁面或換一台電腦登入時,用它還原畫面:所有檔案與最近的工作。"""
        js = db.list_jobs(user["id"], 20)
        ret = cleanup.retention(cfg, perms.effective(db, user))
        return {"items": [jobs.item_view(cfg, i, ret) for i in db.list_items(user["id"])],
                "jobs": [jobs.job_view(db, j, cfg) for j in js]}

    # ------------------------------------------------------------------ 個人設定
    @app.get("/api/prefs")
    def get_prefs(user: dict = user_dep):
        return user.get("prefs") or {}

    @app.put("/api/prefs")
    def put_prefs(body: dict, user: dict = user_dep):
        try:
            prefs = jobs.clean_prefs(cfg, {**jobs.saved_prefs(cfg, user, perms.effective(db, user)), **body},
                                     perms.effective(db, user))
        except (jobs.JobError, TypeError, ValueError) as e:
            raise HTTPException(400, str(e)) from None
        db.update_user(user["id"], prefs=prefs)
        return prefs

    @app.get("/api/glossary")
    def get_glossary(user: dict = user_dep):
        f = cfg.glossary_file(user["id"])
        return {"text": f.read_text(encoding="utf-8-sig", errors="replace") if f.is_file() else ""}

    @app.put("/api/glossary")
    def put_glossary(body: Glossary, user: dict = user_dep):
        text = body.text.replace("\r\n", "\n")
        if len(text.encode("utf-8")) > MAX_GLOSSARY_BYTES:
            raise HTTPException(413, "詞彙表太長(上限 64 KB)")
        f = cfg.glossary_file(user["id"])
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(text.rstrip() + "\n" if text.strip() else "", encoding="utf-8")
        tmp.replace(f)
        from app.translate.glossary import load
        g = load(f)
        return {"ok": True, "terms": len(g.terms), "fixed": len(g.translations)}

    # ------------------------------------------------------------------ 自己的 API 金鑰(第 5 階段)
    @app.get("/api/keys")
    def get_keys(user: dict = user_dep):
        """有沒有設定、末四碼、模型名稱。永遠不回傳金鑰本身。"""
        return {"keys": apikeys.view(db, user["id"]), "allowed": perms.effective(db, user)["api_translate"]}

    @app.put("/api/keys/{provider}")
    def put_key(provider: str, body: KeyInput, user: dict = user_dep):
        if not perms.effective(db, user)["api_translate"]:
            raise HTTPException(403, "你的帳號沒有開通 API 翻譯")
        try:
            key, model = apikeys.clean_input(provider, body.key, body.model)
            return apikeys.save(cfg, db, user["id"], provider, key, model)
        except apikeys.ApiKeyError as e:
            raise HTTPException(400, str(e)) from None

    @app.delete("/api/keys/{provider}")
    def delete_key(provider: str, user: dict = user_dep):
        if provider not in apikeys.PROVIDERS:
            raise HTTPException(404, "不支援的 API")
        db.delete_api_key(user["id"], provider)
        return {"ok": True}

    @app.delete("/api/my-data")
    def delete_my_data(user: dict = user_dep):
        """刪除我的所有檔案、字幕、工作紀錄、詞彙表與 API 金鑰(帳號保留)。處理中的工作要先取消。"""
        if any(j["status"] in ("queued", "running") for j in db.list_jobs(user["id"], 100)):
            raise HTTPException(409, "還有排隊或處理中的工作,請先取消")
        storage.remove_dir(cfg, cfg.user_dir(user["id"]))
        db.delete_user_data(user["id"])
        db.update_user(user["id"], prefs={})
        return {"ok": True}

    @app.put("/api/items")
    async def upload_item(request: Request, name: str, stream: int = 0, user: dict = user_dep):
        """整檔一次上傳(小檔案、測試與舊用戶端用)。網頁介面用下面的分塊上傳。
        這條路不在伺服器抽音訊:原檔留著,處理時由 worker 自己轉 MP3(和桌面版相同)。"""
        name = storage.clean_name(name)
        try:
            length = int(request.headers.get("content-length") or 0)
        except ValueError:
            raise HTTPException(400, "缺少檔案大小") from None
        content = "srt" if name.lower().endswith(".srt") else "original"
        try:
            uploads.check_new(cfg, db, user, name, length, content)
        except uploads.UploadError as e:
            raise HTTPException(e.status, e.message) from None
        tmpdir = cfg.user_dir(user["id"]) / "uploads"
        tmpdir.mkdir(parents=True, exist_ok=True)
        part = tmpdir / f"{storage.new_id()}.part"
        got = 0
        try:
            with open(part, "wb") as f:
                async for chunk in request.stream():
                    got += len(chunk)
                    if got > length:
                        raise HTTPException(413, "檔案超過宣告的大小")
                    await run_in_threadpool(f.write, chunk)
            if got != length:
                raise HTTPException(400, "上傳不完整")
            item = await run_in_threadpool(uploads.finalize, cfg, db, user, part, name, content, None)
        except uploads.UploadError as e:
            part.unlink(missing_ok=True)
            raise HTTPException(e.status, e.message) from None
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        streams = item["info"].get("audio_streams") or []
        if item["kind"] == "media" and 0 < stream < len(streams):
            db.update_item(item["id"], stream=stream)
        view = jobs.item_view(cfg, item)
        view["audio_streams"] = streams
        return view

    # ------------------------------------------------------------------ 分塊上傳(可續傳)
    @app.post("/api/uploads")
    def start_upload(body: UploadStart, user: dict = user_dep):
        name = storage.clean_name(body.name)
        try:
            uploads.check_new(cfg, db, user, name, body.size, body.content)
        except uploads.UploadError as e:
            raise HTTPException(e.status, e.message) from None
        up = db.create_upload(user["id"], storage.new_id(), name, body.size, body.content)
        part = uploads.upload_part(cfg, up)
        part.parent.mkdir(parents=True, exist_ok=True)
        part.touch()
        return {"id": up["id"], "received": 0, "size": up["size"], "chunk_bytes": cfg.chunk_bytes}

    @app.get("/api/uploads")
    def list_uploads(user: dict = user_dep):
        """還沒傳完的上傳(重新選同一個檔案時,可以從上次的位置接著傳)。"""
        return [{"id": u["id"], "name": u["name"], "size": u["size"], "content": u["content"],
                 "received": uploads.upload_received(cfg, u)} for u in db.list_uploads(user["id"])]

    @app.get("/api/uploads/{upload_id}")
    def get_upload(upload_id: str, user: dict = user_dep):
        up = _upload_or_404(user, upload_id)
        return {"id": up["id"], "received": uploads.upload_received(cfg, up), "size": up["size"]}

    @app.put("/api/uploads/{upload_id}")
    async def put_chunk(upload_id: str, offset: int, request: Request, user: dict = user_dep):
        up = _upload_or_404(user, upload_id)
        part = uploads.upload_part(cfg, up)
        have = part.stat().st_size if part.is_file() else 0
        try:
            length = int(request.headers.get("content-length") or 0)
        except ValueError:
            length = -1
        if length <= 0 or length > cfg.chunk_bytes + 1024:
            raise HTTPException(400, "分塊大小不正確")
        if offset != have:              # 位置對不上(例如斷線後重送):告訴瀏覽器伺服器實際收到哪裡
            return JSONResponse({"error": "位置不符", "received": have}, status_code=409)
        if offset + length > up["size"]:
            raise HTTPException(400, "超出宣告的檔案大小")
        got = 0
        with open(part, "ab") as f:
            async for chunk in request.stream():
                got += len(chunk)
                if got > length:
                    break
                await run_in_threadpool(f.write, chunk)
        received = part.stat().st_size
        if got != length:               # 傳到一半斷線:把這一塊丟掉,下次從這塊的開頭重傳
            with open(part, "r+b") as f:
                f.truncate(have)
            received = have
        return {"received": received, "size": up["size"]}

    @app.post("/api/uploads/{upload_id}/finish")
    async def finish_upload(upload_id: str, user: dict = user_dep):
        up = _upload_or_404(user, upload_id)
        part = uploads.upload_part(cfg, up)
        received = uploads.upload_received(cfg, up)
        if received != up["size"]:
            return JSONResponse({"error": "檔案還沒傳完", "received": received}, status_code=409)
        db.delete_upload(user["id"], upload_id)
        try:
            item = await run_in_threadpool(uploads.finalize, cfg, db, user, part, up["name"], up["content"],
                                           extractor)
        except uploads.UploadError as e:
            raise HTTPException(e.status, e.message) from None
        view = jobs.item_view(cfg, item)
        view["audio_streams"] = item["info"].get("audio_streams") or []
        return view

    @app.delete("/api/uploads/{upload_id}")
    def cancel_upload(upload_id: str, user: dict = user_dep):
        up = _upload_or_404(user, upload_id)
        uploads.upload_part(cfg, up).unlink(missing_ok=True)
        db.delete_upload(user["id"], upload_id)
        return {"ok": True}

    @app.post("/api/items/{item_id}/track")
    def choose_track(item_id: str, body: TrackChoice, user: dict = user_dep):
        """改傳原檔、而且有多條音軌時:選好音軌,伺服器才開始抽音訊。"""
        item = _item_or_404(user, item_id)
        streams = (item.get("info") or {}).get("audio_streams") or []
        if item["state"] != "choose_track":
            raise HTTPException(409, "這個檔案不需要選擇音軌")
        if not 0 <= body.stream < len(streams):
            raise HTTPException(400, "沒有這條音軌")
        db.update_item(item_id, state="extracting", stream=body.stream, progress=0.0, message="伺服器抽音訊中")
        extractor.submit(user["id"], item_id, body.stream)
        return jobs.item_view(cfg, db.get_item(user["id"], item_id))

    # ------------------------------------------------------------------ 管理頁(第 4.5 階段)
    # 只給管理者看帳號、權限與用量;不提供任何檔案名稱或字幕內容。
    admin_dep = Depends(auth.admin_user)

    def admin_user_view(u: dict, tiers: dict) -> dict:
        st = db.user_stats(u["id"])
        p = perms.effective(db, u, tiers)
        return {"id": u["id"], "name": u["name"], "email": u["email"], "provider": u["provider"],
                "status": u["status"], "tier": p["tier"], "is_admin": bool(u["is_admin"]),
                "created_at": u["created_at"], "last_login": u.get("last_login"),
                "last_seen": u.get("last_seen") or u.get("last_login"),
                "overrides": u.get("overrides") or {}, "perms": {k: p[k] for k in perms.FIELDS},
                "items": st["items"], "jobs": st["jobs"], "seconds_total": st["seconds_total"],
                "storage_bytes": uploads.user_bytes(cfg, u["id"]), "storage_limit": p["storage_bytes"],
                "today_seconds": db.seconds_since(u["id"], uploads.day_start(cfg)),
                "daily_limit": p["daily_seconds"]}

    @app.get("/api/admin/users")
    def admin_users(admin: dict = admin_dep):
        tiers = perms.tier_defaults(db)
        out = [admin_user_view(u, tiers) for u in db.list_users()]
        order = {"pending": 0, "approved": 1, "blocked": 2}
        out.sort(key=lambda x: (order.get(x["status"], 3), -(x["created_at"] or 0)))
        return {"users": out, "require_approval": cfg.require_approval, "day_seconds": cfg.day_seconds,
                "tiers": tiers, "tier_labels": perms.TIER_LABELS, "fields": perms.field_list()}

    @app.post("/api/admin/users/{user_id}")
    def admin_update_user(user_id: int, body: UserUpdate, admin: dict = admin_dep):
        """核准(順便選等級)、停用/恢復、改等級、指定管理者、個別設定。
        停用後該帳號立即無法使用(包括已經登入的瀏覽器),排隊中與處理中的工作一併取消。"""
        target = db.get_user(user_id)
        if target is None:
            raise HTTPException(404, "找不到這個帳號")
        changes: dict = {}
        if body.status is not None:
            if body.status not in ("approved", "blocked", "pending"):
                raise HTTPException(400, "不支援的狀態")
            if user_id == admin["id"] and body.status != "approved":
                raise HTTPException(400, "不能停用自己的帳號")
            changes["status"] = body.status
        if body.tier is not None:
            if body.tier not in perms.TIERS:
                raise HTTPException(400, "不支援的等級")
            changes["tier"] = body.tier
        if body.is_admin is not None:
            if user_id == admin["id"] and not body.is_admin:
                raise HTTPException(400, "不能取消自己的管理者身分")
            changes["is_admin"] = int(body.is_admin)
        if body.overrides is not None:
            try:
                changes["overrides"] = perms.clean_overrides(body.overrides)
            except perms.PermError as e:
                raise HTTPException(400, str(e)) from None
        if not changes:
            raise HTTPException(400, "沒有要修改的項目")
        db.update_user(user_id, **changes)
        if changes.get("status") == "blocked":
            for j in db.list_jobs(user_id, 100):
                if j["status"] in ("queued", "running") and db.request_cancel(user_id, j["id"]) == "canceled":
                    for e in j["plan"]:
                        it = db.get_item(user_id, e["item_id"])
                        if it and it["state"] == "queued":
                            db.update_item(it["id"], state="canceled", message="已取消")
                    db.add_event(j["id"], user_id, "job", {"status": "canceled"})
        log.info("管理者 %s 修改帳號 %s:%s", admin["id"], user_id,
                 json.dumps(changes, ensure_ascii=False, default=str))
        return admin_user_view(db.get_user(user_id), perms.tier_defaults(db))

    @app.put("/api/admin/tiers/{tier}")
    def admin_set_tier(tier: str, body: dict, admin: dict = admin_dep):
        """修改等級的預設值;立即套用到這個等級所有沒有個別設定的人。"""
        if tier not in perms.TIERS:
            raise HTTPException(404, "沒有這個等級")
        try:
            values = perms.clean_tier_values(body)
        except perms.PermError as e:
            raise HTTPException(400, str(e)) from None
        db.set_tier(tier, values)
        log.info("管理者 %s 修改等級 %s 的預設值:%s", admin["id"], tier, json.dumps(values, ensure_ascii=False))
        return perms.tier_defaults(db)[tier]

    @app.post("/api/admin/cleanup")
    def admin_cleanup(admin: dict = admin_dep):
        """立即執行一輪到期清除(平常每小時自動執行)。"""
        stats = cleaner.run_once()
        log.info("管理者 %s 手動執行到期清除", admin["id"])
        return stats

    @app.get("/api/admin/queue")
    def admin_queue(admin: dict = admin_dep):
        """處理中與排隊中的工作(依輪到的順序)。只顯示是誰、幾個檔案、多長,不顯示檔名。"""
        names = {u["id"]: u for u in db.list_users()}
        out = []
        for j in db.running_jobs() + db.queued_jobs():
            u = names.get(j["user_id"]) or {}
            ids = [e["item_id"] for e in j["plan"]]
            out.append({"id": j["id"], "status": j["status"], "user": u.get("name") or "?",
                        "email": u.get("email"), "files": len(ids), "seconds": db.item_durations(ids),
                        "priority": bool(j.get("priority")), "created_at": j["created_at"],
                        "started_at": j["started_at"], "wait_seconds": jobs.estimate_wait(db, cfg, j)})
        return {"jobs": out, "priority_max_wait_min": cfg.priority_max_wait_s // 60}

    @app.get("/api/usage")
    def get_usage(user: dict = user_dep):
        return uploads.usage(cfg, db, user)

    @app.delete("/api/items/{item_id}")
    def delete_item(item_id: str, user: dict = user_dep):
        item = _item_or_404(user, item_id)
        if item["state"] in jobs.ACTIVE_ITEM_STATES:
            raise HTTPException(409, "這個檔案正在處理中,請先取消工作")
        storage.remove_item_files(cfg, item)
        db.delete_item(user["id"], item_id)
        return {"ok": True}

    @app.get("/api/items/{item_id}/download")
    def download(item_id: str, which: str = "srt", user: dict = user_dep):
        item = _item_or_404(user, item_id)
        if which not in ("srt", "translated", "mp3", "txt"):
            raise HTTPException(400, "不支援的下載類型")
        p = storage.output_file(cfg, item, which)
        if p is None:
            raise HTTPException(404, "沒有這個檔案")
        fname = storage.download_name(item, p.name)
        return FileResponse(p, filename=None, headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}",
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/api/items/{item_id}/preview")
    def item_preview(item_id: str, which: str = "srt", user: dict = user_dep):
        item = _item_or_404(user, item_id)
        if which not in ("srt", "translated"):
            raise HTTPException(400, "不支援的預覽類型")
        p = storage.output_file(cfg, item, which)
        if p is None and which == "srt" and item["kind"] == "srt":
            p = storage.stored_path(cfg, item)          # 上傳的字幕檔本身
        if p is None or not p.is_file():
            raise HTTPException(404, "沒有這個字幕")
        data = preview.preview(p)
        data["name"] = storage.download_name(item, p.name) if p.name != item["stored"] else item["name"]
        return JSONResponse(data, headers=NO_STORE)

    @app.get("/api/download-zip")
    def download_zip(ids: str, which: str = "srt,translated", user: dict = user_dep):
        """把選取檔案的字幕打包成一個 zip 下載。"""
        kinds = [k for k in which.split(",") if k in ("srt", "translated", "txt")]
        buf = io.BytesIO()
        used: set[str] = set()
        n = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for item_id in ids.split(",")[:200]:
                item = db.get_item(user["id"], item_id.strip())
                if not item:
                    continue
                for k in kinds:
                    p = storage.output_file(cfg, item, k)
                    if p is None:
                        continue
                    name = storage.download_name(item, p.name)
                    base, i = name, 2
                    while name in used:              # 同名檔案加上編號
                        stem, dot, ext = base.rpartition(".")
                        name = f"{stem} ({i}).{ext}" if dot else f"{base} ({i})"
                        i += 1
                    used.add(name)
                    z.write(p, name)
                    n += 1
        if n == 0:
            raise HTTPException(404, "選取的檔案還沒有字幕")
        return Response(buf.getvalue(), media_type="application/zip", headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote('字幕.zip')}", **NO_STORE})

    # ------------------------------------------------------------------ 工作
    @app.post("/api/jobs")
    def create_job(body: JobRequest, user: dict = user_dep):
        try:
            return jobs.create_job(cfg, db, user, body.item_ids, body.mode, body.options)
        except jobs.JobError as e:
            raise HTTPException(400, str(e)) from None

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, user: dict = user_dep):
        return jobs.job_view(db, _job_or_404(user, job_id), cfg)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str, user: dict = user_dep):
        _job_or_404(user, job_id)
        res = db.request_cancel(user["id"], job_id)
        if res is None:
            raise HTTPException(409, "這個工作已經結束")
        if res == "canceled":      # 還在排隊:直接取消,檔案恢復成可再處理
            job = db.get_job(user["id"], job_id)
            for e in job["plan"]:
                it = db.get_item(user["id"], e["item_id"])
                if it and it["state"] == "queued":
                    db.update_item(it["id"], state="canceled", message="已取消")
            db.add_event(job_id, user["id"], "job", {"status": "canceled"})
        return {"status": res}

    @app.get("/api/jobs/{job_id}/events")
    def job_events(job_id: str, request: Request, after: int = 0, user: dict = user_dep):
        """進度推播(Server-Sent Events)。斷線重連時瀏覽器會自動帶 Last-Event-ID,從上次的位置接續。"""
        _job_or_404(user, job_id)
        try:
            last = max(after, int(request.headers.get("last-event-id") or 0))
        except ValueError:
            last = after
        return StreamingResponse(_sse(db, cfg, user["id"], job_id, last), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ 共用
    def _item_or_404(user: dict, item_id: str) -> dict:
        item = db.get_item(user["id"], item_id)
        if item is None:
            raise HTTPException(404, "找不到這個檔案")
        return item

    def _upload_or_404(user: dict, upload_id: str) -> dict:
        up = db.get_upload(user["id"], upload_id)
        if up is None:
            raise HTTPException(404, "找不到這次上傳")
        return up

    def _job_or_404(user: dict, job_id: str) -> dict:
        job = db.get_job(user["id"], job_id)
        if job is None:
            raise HTTPException(404, "找不到這個工作")
        return job

    @app.exception_handler(StarletteHTTPException)
    async def http_exc(_req: Request, exc: StarletteHTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    return app


def _sse(db: DB, cfg: Config, user_id: int, job_id: str, last: int):
    last_ping = last_pos = 0.0
    while True:
        evs = db.events_after(user_id, job_id, last)
        for e in evs:
            last = e["seq"]
            yield f"id: {last}\nevent: {e['kind']}\ndata: {json.dumps(e['data'], ensure_ascii=False)}\n\n"
        job = db.get_job(user_id, job_id)
        if job is None:
            return
        now = time.monotonic()
        if job["status"] == "queued" and now - last_pos > 2:
            last_pos = now
            info = {"position": db.queue_position(job_id), "wait_seconds": jobs.estimate_wait(db, cfg, job)}
            yield f"event: queue\ndata: {json.dumps(info)}\n\n"
        if not evs:
            if job["status"] not in ("queued", "running"):
                if not db.events_after(user_id, job_id, last, 1):     # 結束後再確認一次沒有漏掉的事件
                    yield f"event: end\ndata: {json.dumps({'status': job['status']})}\n\n"
                    return
                continue
            if now - last_ping > 15:
                last_ping = now
                yield ": ping\n\n"
            time.sleep(0.5)
