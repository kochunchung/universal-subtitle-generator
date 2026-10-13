"""工作的建立與檢視:把使用者的選項轉成交給 worker.run 的工作清單與設定。

使用者只能改「安全的」選項;批次大小、覆蓋、離線合併等進階項目由伺服器固定,
避免使用者把顯存撲爆。設定裡不放任何金鑰。
"""
from __future__ import annotations

import copy

from app import settings as settings_mod
from app.translate.common import LANG_NAMES, TARGETS
from app.web import perms, storage
from app.web.config import Config
from app.web.db import DB
from app.web.planner import MODES, plan_item

SOURCE_LANGS = ["auto", "zh", "en", "ja", "ko"]
API_ENGINES = ("anthropic", "openai")
ACTIVE_ITEM_STATES = ("queued", "running")
PREPARING_STATES = ("extracting", "choose_track")      # 上傳後還沒準備好的檔案(備援路線)


WEB_ENGINE_LABELS = {"openai": "OpenAI API"}     # 網頁版只連官方 OpenAI,不叫「相容 API」


def engine_label(e: str) -> str:
    return WEB_ENGINE_LABELS.get(e) or settings_mod.TRANSLATION_ENGINES.get(e, e)


class JobError(ValueError):
    """可以直接顯示給使用者的錯誤。"""


def allowed_engines(cfg: Config, p: dict | None) -> list[str]:
    """這個人能用的翻譯引擎:伺服器開放的,再依權限的「API 翻譯」「離線翻譯」過濾。p=None 表示不過濾。"""
    out = []
    for e in cfg.engines:
        if e not in settings_mod.TRANSLATION_ENGINES:
            continue
        if p is not None and not p["api_translate" if e in API_ENGINES else "offline_translate"]:
            continue
        out.append(e)
    return out


def public_options(cfg: Config, p: dict | None = None) -> dict:
    engines = allowed_engines(cfg, p)
    return {
        "whisper_models": list(cfg.whisper_models),
        "engines": engines,
        "engine_labels": {e: engine_label(e) for e in engines},
        "source_languages": SOURCE_LANGS,
        "targets": TARGETS if engines else [],          # 沒有任何翻譯權限:只能轉錄
        "lang_names": LANG_NAMES,
        "defaults": {
            "whisper_model": cfg.whisper_models[0],
            "source_language": "auto",
            "translate_to": None,
            "subtitle_mode": "translated",
            "translation_engine": _default_engine(cfg, engines),
            "opencc_s2twp": True, "vad": True, "write_txt": False,
        },
    }


def _default_engine(cfg: Config, engines: list[str]) -> str:
    pool = engines or list(cfg.engines)
    return "m2m100" if "m2m100" in pool else pool[0]


def build_settings(cfg: Config, opts: dict, p: dict | None = None) -> dict:
    """使用者選項 -> worker.run 的設定。不認得或不合法的選項直接丟錯,不默默忽略。
    p 是這個人的實際權限(perms.effective);用來限制能選的翻譯引擎。"""
    engines = allowed_engines(cfg, p)
    base = settings_mod.load()
    s = copy.deepcopy(settings_mod.DEFAULTS)
    s["calibration"] = base.get("calibration", {})        # 只沿用全機共用的顯存實測值
    s["whisper_model"] = cfg.whisper_models[0]
    s["translation_engine"] = _default_engine(cfg, engines)
    # 由伺服器固定(使用者不能改)
    s["overwrite"] = False
    s["offline_merge"] = False
    s["delete_mp3"] = False
    s["batch_size"] = cfg.batch_size
    # API 翻譯:網頁版只連官方網址(不讓使用者叫伺服器去連任意位址);OpenAI 的模型名稱在建立工作時填
    s["api"] = {"anthropic_model": "", "openai_base_url": "", "openai_model": ""}

    def pick(key: str, allowed) -> None:
        if key in opts and opts[key] is not None:
            if opts[key] not in allowed:
                raise JobError(f"不支援的選項:{key}")
            s[key] = opts[key]

    pick("whisper_model", cfg.whisper_models)
    pick("source_language", SOURCE_LANGS)
    pick("subtitle_mode", ("translated", "bilingual"))
    eng = opts.get("translation_engine")
    if eng is not None:
        if eng not in cfg.engines or eng not in settings_mod.TRANSLATION_ENGINES:
            raise JobError("不支援的選項:translation_engine")
        if eng not in engines:
            raise JobError(f"你的帳號沒有開通「{engine_label(eng)}」,請改選其他翻譯引擎")
        s["translation_engine"] = eng
    if "translate_to" in opts:
        if opts["translate_to"] not in (None, "", *TARGETS):
            raise JobError("不支援的翻譯目標")
        s["translate_to"] = opts["translate_to"] or None
        if s["translate_to"] and not engines:
            raise JobError("你的帳號沒有開通翻譯功能,請把「翻譯目標」改成不翻譯,或請管理者開通")
    for k in ("opencc_s2twp", "vad", "write_txt"):
        if k in opts and opts[k] is not None:
            s[k] = bool(opts[k])
    seg = s["segmenter"]
    for k, lo, hi in (("max_chars_cjk", 8, 40), ("max_chars_latin", 20, 80)):
        if opts.get(k) is not None:
            try:
                seg[k] = max(lo, min(hi, int(opts[k])))
            except (TypeError, ValueError):
                raise JobError(f"選項格式錯誤:{k}") from None
    return s


PREF_KEYS = ("whisper_model", "source_language", "translate_to", "subtitle_mode", "translation_engine",
             "opencc_s2twp", "vad", "write_txt", "max_chars_cjk", "max_chars_latin", "upload_mode")
UPLOAD_MODES = ("browser", "original")     # 影片上傳方式:瀏覽器先抽聲音 / 直接上傳原檔(要有改傳原檔的權限)


def clean_prefs(cfg: Config, prefs: dict, perm: dict | None = None) -> dict:
    """檢查並只保留認得的偏好設定;不合法就丟錯(和送出工作時同一套檢查)。"""
    p = {k: prefs[k] for k in PREF_KEYS if k in prefs}
    if "upload_mode" in p and p["upload_mode"] not in UPLOAD_MODES:
        raise JobError("不支援的選項:upload_mode")
    s = build_settings(cfg, {k: v for k, v in p.items() if k != "upload_mode"}, perm)
    for k in ("max_chars_cjk", "max_chars_latin"):       # 存調整到合理範圍後的值
        if k in p:
            p[k] = s["segmenter"][k]
    return p


def saved_prefs(cfg: Config, user: dict, perm: dict) -> dict:
    """存的偏好;權限被收回的選項(例如翻譯引擎、翻譯目標)拿掉,改用預設值。"""
    saved = {k: v for k, v in (user.get("prefs") or {}).items() if k in PREF_KEYS}
    engines = allowed_engines(cfg, perm)
    if saved.get("translation_engine") not in engines:
        saved.pop("translation_engine", None)
    if not engines:
        saved.pop("translate_to", None)
    return saved


def create_job(cfg: Config, db: DB, user: dict, item_ids: list[str], mode: str, opts: dict) -> dict:
    """依「開始處理」的規則建立一個工作。回傳 {"job": ..., "skipped": [...]}。
    選項 = 這個人存的偏好(設定頁的每行字數等)+ 這次送出的選項(後者優先)。"""
    if mode not in MODES:
        raise JobError("不支援的處理模式")
    perm = perms.effective(db, user)
    opts = {**saved_prefs(cfg, user, perm), **(opts or {})}
    seen: set[str] = set()
    ids = [i for i in item_ids if not (i in seen or seen.add(i))]
    items = db.get_items(user["id"], ids)
    if not items:
        raise JobError("沒有需要處理的檔案。請先上傳影片、音檔或字幕檔。")
    s = build_settings(cfg, opts, perm)
    tgt = s.get("translate_to")
    eng = s["translation_engine"]
    if tgt and eng in API_ENGINES:
        key = db.get_api_keys(user["id"]).get(eng)
        label = engine_label(eng)
        if key is None:
            raise JobError(f"要用「{label}」翻譯,請先到「設定」頁填你自己的金鑰")
        s["api"]["openai_model"] = key["model"] if eng == "openai" else ""

    active = [j for j in db.list_jobs(user["id"], 100) if j["status"] in ("queued", "running")]
    if len(active) >= perm["queued_jobs"]:
        raise JobError(f"同時排隊的工作已達上限({perm['queued_jobs']} 個),請等前面的完成或先取消")

    plan: list[dict] = []
    skipped: list[dict] = []
    for it in items:
        if it["state"] in ACTIVE_ITEM_STATES:
            skipped.append({"item_id": it["id"], "name": it["name"], "reason": "已經在處理佇列中"})
            continue
        if it["state"] in PREPARING_STATES:
            reason = "請先選擇要轉錄的音軌" if it["state"] == "choose_track" else "伺服器還在抽音訊,請稍候"
            skipped.append({"item_id": it["id"], "name": it["name"], "reason": reason})
            continue
        p = plan_item(cfg, it, mode, s)
        if p.found_translated:
            db.merge_item_outputs(it["id"], {"translated": p.found_translated})
        if p.job is None:
            if p.reason:
                skipped.append({"item_id": it["id"], "name": it["name"], "reason": p.reason})
            continue
        entry = dict(p.job, idx=len(plan), item_id=it["id"], stream=int(it.get("stream") or 0))
        plan.append(entry)
    if not plan:
        if mode != "auto":
            raise JobError("選取的檔案沒有需要處理的項目。" + _why(skipped))
        raise JobError("沒有需要處理的檔案,清單中的工作都已經完成。\n"
                       "・要翻譯已轉錄的字幕:先選擇「翻譯目標」,再按「開始處理」(不會重新轉錄)。\n"
                       "・要重做:在檔案旁選「重新轉錄」或「重新翻譯」。" + _why(skipped))

    # 每日額度:只算這次要轉錄的音訊長度(只翻譯不算)
    from app.web import uploads

    by_id = {it["id"]: it for it in items}
    seconds = sum(float(by_id[e["item_id"]].get("duration") or 0) for e in plan if e["kind"] == "media")
    try:
        uploads.check_daily(cfg, db, user, seconds)
    except uploads.UploadError as e:
        raise JobError(e.message) from None
    job_id = storage.new_id()
    job = db.create_job(user["id"], job_id, mode, plan, s, seconds, priority=perm["priority"])
    for e in plan:
        fields = dict(state="queued", progress=0.0, message="")
        if e["kind"] == "media" and e.get("overwrite"):
            fields["outputs"] = {}          # 重新轉錄:舊字幕不再視為有效
        db.update_item(e["item_id"], **fields)
    n_tr = sum(e["kind"] == "media" for e in plan)
    parts = ([f"轉錄 {n_tr} 個"] if n_tr else []) + ([f"只翻譯 {len(plan) - n_tr} 個"] if len(plan) > n_tr else [])
    db.add_event(job_id, user["id"], "log", {"text": "開始處理:" + "、".join(parts)
                                              + (f"(翻譯成 {tgt})" if tgt else "")})
    for r in skipped:
        db.add_event(job_id, user["id"], "log", {"text": f"略過 {r['name']}:{r['reason']}"})
    return {"job": job_view(db, job, cfg), "skipped": skipped}


def _why(skipped: list[dict]) -> str:
    return ("\n\n" + "\n".join(f"{r['name']}:{r['reason']}" for r in skipped[:8])) if skipped else ""


# ---------------------------------------------------------------------- 檢視(給 API 回傳)
def item_view(cfg: Config, item: dict, ret: dict | None = None) -> dict:
    """ret = cleanup.retention(...):有給的話,一併回傳音訊與字幕的刪除時間。"""
    outs = [k for k in ("srt", "translated", "mp3") if storage.output_file(cfg, item, k)]
    expires = None
    if ret is not None:
        from app.web import cleanup

        expires = cleanup.deadlines(cfg, item, ret)
    return {
        "expires": expires, "audio_expired": bool((item.get("info") or {}).get("audio_expired")),
        "id": item["id"], "name": item["name"], "kind": item["kind"], "size": item["size"],
        "state": item["state"], "progress": item["progress"], "message": item["message"],
        "outputs": outs, "created_at": item["created_at"], "duration": item.get("duration") or 0,
        # 改傳原檔且有多條音軌時,讓使用者選
        "tracks": (item.get("info") or {}).get("audio_streams") or [] if item["state"] == "choose_track" else [],
    }


def estimate_wait(db: DB, cfg: Config, job: dict) -> int | None:
    """排隊中的工作大約還要等幾秒才開始:前面工作的音訊總長度 ÷ 處理速度(粗估,只供參考)。"""
    if job["status"] != "queued":
        return None
    total = 0.0
    for j in db.queued_jobs_before(job["id"]):
        ids = [e["item_id"] for e in j["plan"]]
        dur = db.item_durations(ids)
        if j["status"] == "running" and ids:
            done = [(db.get_item(j["user_id"], i) or {}).get("progress") or 0 for i in ids]
            dur *= max(0.0, 1.0 - sum(done) / len(done))
        total += dur / max(cfg.speed_x, 1.0) + 20      # 每個工作另加載入模型等固定時間
    return int(total)


def job_view(db: DB, job: dict, cfg: Config | None = None) -> dict:
    return {
        "id": job["id"], "status": job["status"], "mode": job["mode"], "error": job["error"],
        "created_at": job["created_at"], "started_at": job["started_at"], "finished_at": job["finished_at"],
        "position": db.queue_position(job["id"]),
        "wait_seconds": estimate_wait(db, cfg, job) if cfg else None,
        "item_ids": [e["item_id"] for e in job["plan"]],
        "translate_to": job["settings"].get("translate_to"),
    }
