"""會員等級與個別權限(第 4.5 階段)。

權限分三層,由上往下決定:
  1. 帳號狀態(待核准/可使用/停用):auth.current_user 負責,只有「可使用」能用任何功能。
  2. 等級(一般/付費):一整套預設值,存在資料庫的 tiers 表,管理頁可以直接改,改了立即套用。
  3. 個別設定:users.overrides 裡有填的項目蓋過等級預設值;沒填的照等級。

effective() 是唯一計算「這個人現在實際的權限」的地方,上傳、建立工作、排隊都問它。
之後加新的權限項目:在 FIELDS 與 DEFAULTS 各加一行,再到用得到的地方讀它即可。
"""
from __future__ import annotations

from typing import Any

from app.web.db import DB

TIERS = ("free", "paid")
TIER_LABELS = {"free": "一般會員", "paid": "付費會員"}

# key: (類型, 名稱, 單位, 最小, 最大)。類型 int 或 bool;bool 的最小最大不用。
FIELDS: dict[str, tuple[str, str, str, int, int]] = {
    "audio_mb":          ("int",  "瀏覽器抽出的音訊(單檔)", "MB", 1, 4096),
    "original_allowed":  ("bool", "瀏覽器處理不了時改傳原檔", "", 0, 0),
    "original_mb":       ("int",  "改傳原檔的單檔上限", "MB", 1, 102400),
    "duration_min":      ("int",  "單一檔案長度", "分鐘", 1, 1440),
    "storage_mb":        ("int",  "每人空間", "MB", 10, 1048576),
    "daily_minutes":     ("int",  "每天處理時數", "分鐘", 0, 1440),
    "queued_jobs":       ("int",  "同時排隊的工作", "個", 1, 20),
    "priority":          ("bool", "優先排隊", "", 0, 0),
    "keep_audio_days":   ("int",  "音訊保存天數", "天", 1, 3650),
    "keep_srt_days":     ("int",  "字幕保存天數", "天", 1, 3650),
    "api_translate":     ("bool", "API 翻譯(用自己的金鑰)", "", 0, 0),
    "offline_translate": ("bool", "離線翻譯(M2M100)", "", 0, 0),
}

# 10/8 確認的數字(規劃文件第 14 節)。資料庫裡沒有存過時用這組。
DEFAULTS: dict[str, dict[str, Any]] = {
    "free": {"audio_mb": 200, "original_allowed": False, "original_mb": 6144, "duration_min": 180,
             "storage_mb": 1024, "daily_minutes": 120, "queued_jobs": 3, "priority": False,
             "keep_audio_days": 3, "keep_srt_days": 7, "api_translate": True, "offline_translate": True},
    "paid": {"audio_mb": 300, "original_allowed": True, "original_mb": 6144, "duration_min": 300,
             "storage_mb": 10240, "daily_minutes": 300, "queued_jobs": 3, "priority": True,
             "keep_audio_days": 3, "keep_srt_days": 7, "api_translate": True, "offline_translate": True},
}


class PermError(ValueError):
    """管理者輸入的數值不合法(可以直接顯示)。"""


def _value(key: str, v: Any) -> Any:
    typ, label, unit, lo, hi = FIELDS[key]
    if typ == "bool":
        if isinstance(v, bool):
            return v
        if v in (0, 1, "0", "1", "true", "false"):
            return v in (1, "1", "true")
        raise PermError(f"「{label}」只能是開或關")
    if isinstance(v, bool):
        raise PermError(f"「{label}」要填數字")
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        raise PermError(f"「{label}」要填整數") from None
    if not lo <= n <= hi:
        raise PermError(f"「{label}」要在 {lo}~{hi} {unit} 之間")
    return n


def clean_tier_values(values: dict) -> dict:
    """等級預設值:每一項都要有。"""
    if not isinstance(values, dict):
        raise PermError("格式錯誤")
    unknown = set(values) - set(FIELDS)
    if unknown:
        raise PermError("不認得的項目:" + "、".join(sorted(unknown)))
    missing = [FIELDS[k][1] for k in FIELDS if k not in values]
    if missing:
        raise PermError("缺少:" + "、".join(missing))
    return {k: _value(k, values[k]) for k in FIELDS}


def clean_overrides(values: dict) -> dict:
    """個別設定:只留有填的項目;None 或空字串 = 照等級。"""
    if not isinstance(values, dict):
        raise PermError("格式錯誤")
    unknown = set(values) - set(FIELDS)
    if unknown:
        raise PermError("不認得的項目:" + "、".join(sorted(unknown)))
    return {k: _value(k, v) for k, v in values.items() if v is not None and v != ""}


def tier_defaults(db: DB) -> dict[str, dict]:
    stored = db.get_tiers()
    return {t: {**DEFAULTS[t], **{k: v for k, v in stored.get(t, {}).items() if k in FIELDS}} for t in TIERS}


def effective(db: DB, user: dict, tiers: dict[str, dict] | None = None) -> dict:
    """這個人現在實際的權限:等級預設值 + 個別設定,另外附上換算好的位元組與秒數。"""
    tiers = tiers or tier_defaults(db)
    tier = user.get("tier") if user.get("tier") in TIERS else "free"
    p = dict(tiers[tier])
    for k, v in (user.get("overrides") or {}).items():
        if k in FIELDS:
            p[k] = v
    p.update(
        tier=tier, tier_label=TIER_LABELS[tier],
        audio_bytes=p["audio_mb"] * 2**20, original_bytes=p["original_mb"] * 2**20,
        duration_s=p["duration_min"] * 60, storage_bytes=p["storage_mb"] * 2**20,
        daily_seconds=p["daily_minutes"] * 60,
    )
    return p


def field_list() -> list[dict]:
    """給管理頁畫表單用。"""
    return [{"key": k, "type": t, "label": label, "unit": unit, "min": lo, "max": hi}
            for k, (t, label, unit, lo, hi) in FIELDS.items()]
