"""使用者檔案在磁碟上的位置與命名。

規則:
- 伺服器上的檔名一律是安全的固定名稱(<項目編號>.mp4、<項目編號>.en.srt),不使用使用者提供的檔名,
  避開中文、空白、特殊字元與路徑穿越問題;用項目編號當主檔名,日誌裡才分得出是哪一個檔案。
- 使用者看到的原檔名只存在資料庫,下載時再換回去。
- 每個項目一個資料夾:data\\users\\<使用者編號>\\items\\<項目編號>\\ 。核心的輸出檔(mp3、srt)寫在同一個資料夾。
"""
from __future__ import annotations

import re
import secrets
import shutil
from pathlib import Path

from app.core.media import AUDIO_EXTS, VIDEO_EXTS
from app.translate import common
from app.web.config import Config

SRT_EXTS = {".srt"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS
_BAD = re.compile(r'[\x00-\x1f\\/:*?"<>|]')


def new_id() -> str:
    return secrets.token_hex(12)


def clean_name(name: str) -> str:
    """只保留可以安全顯示的檔名(去掉路徑與控制字元),過長就截短。"""
    name = _BAD.sub("_", Path(str(name).replace("\\", "/")).name).strip(" .") or "file"
    p = Path(name)
    stem, ext = p.stem[:120], p.suffix[:12]
    return stem + ext


def classify(name: str) -> tuple[str | None, str]:
    """回傳(類型, 副檔名)。類型是 media / srt;不支援的副檔名回傳 None。"""
    ext = Path(name).suffix.lower()
    if ext in MEDIA_EXTS:
        return "media", ext
    if ext in SRT_EXTS:
        return "srt", ext
    return None, ext


def stored_name(name: str, kind: str, item_id: str) -> str:
    ext = Path(name).suffix.lower()
    if kind == "media":
        return f"{item_id}{ext}"
    _stem, tag = common.srt_stem_and_lang(Path(clean_name(name)))
    return f"{item_id}.{tag}.srt" if tag and re.fullmatch(r"[A-Za-z\-]{2,10}", tag) else f"{item_id}.srt"


def item_dir(cfg: Config, item: dict) -> Path:
    return cfg.item_dir(item["user_id"], item["id"])


def stored_path(cfg: Config, item: dict) -> Path:
    return item_dir(cfg, item) / item["stored"]


def _inside(base: Path, p: Path) -> bool:
    try:
        p.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def output_file(cfg: Config, item: dict, key: str) -> Path | None:
    """某個輸出檔(mp3 / srt / translated)的實際路徑;不存在或不在項目資料夾內就是 None。"""
    rel = (item.get("outputs") or {}).get(key)
    if not rel:
        return None
    base = item_dir(cfg, item)
    p = base / Path(rel).name                    # 只取檔名,絕不接受路徑
    return p if p.is_file() and _inside(base, p) else None


def to_output_name(cfg: Config, item: dict, abs_path: str | Path) -> str | None:
    """worker 回報的絕對路徑 -> 資料庫存的檔名;不在項目資料夾內的一律丟棄。"""
    base = item_dir(cfg, item)
    p = Path(abs_path)
    return p.name if p.parent.resolve() == base.resolve() else None


def download_name(item: dict, output_basename: str) -> str:
    """伺服器檔名 -> 使用者看到的下載檔名(把項目編號換回原檔名的主檔名)。"""
    orig_stem = display_stem(item)
    if output_basename.startswith(item["id"] + "."):
        return orig_stem + output_basename[len(item["id"]):]
    return output_basename


def display_stem(item: dict) -> str:
    if item["kind"] == "media":
        return Path(item["name"]).stem
    return common.srt_stem_and_lang(Path(item["name"]))[0].name


def remove_dir(cfg: Config, d: Path) -> None:
    """只刪除資料目錄底下的資料夾。"""
    if d.is_dir() and _inside(cfg.data_dir, d) and d.resolve() != cfg.data_dir.resolve():
        shutil.rmtree(d, ignore_errors=True)


def remove_item_files(cfg: Config, item: dict) -> None:
    remove_dir(cfg, item_dir(cfg, item))
