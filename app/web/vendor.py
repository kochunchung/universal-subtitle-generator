"""瀏覽器抽音訊用的 ffmpeg.wasm 元件:第一次需要時從 CDN 下載,存在 data\\vendor,之後都從自己的網站提供。

為什麼不直接讓瀏覽器去 CDN 抓:ffmpeg.wasm 要建立 Web Worker,瀏覽器不允許跨網域建立,所以要同網域提供。
元件約 32 MB(核心 wasm),超過單一檔案的傳送限制,所以不放進程式資料夾,改成第一次使用時下載。
伺服器沒有網路時,可以先在有網路的電腦執行 python -m app.web.vendor,再把 data\\vendor 複製過去。
"""
from __future__ import annotations

import shutil
import sys
import threading
import urllib.request
from pathlib import Path

from app.paths import ROOT

CACHE = ROOT / "data" / "vendor"
SPIKE_CACHE = ROOT / "data" / "spike" / "vendor"
PACKAGES = {
    "ffmpeg": ("@ffmpeg/ffmpeg", "0.12.10", "dist/esm",
               ("index.js", "classes.js", "const.js", "errors.js", "types.js", "utils.js", "worker.js")),
    "util": ("@ffmpeg/util", "0.12.1", "dist/esm", ("index.js", "const.js", "errors.js", "types.js")),
    "core": ("@ffmpeg/core", "0.12.6", "dist/esm", ("ffmpeg-core.js", "ffmpeg-core.wasm")),
}
CDNS = ("https://unpkg.com/{pkg}@{ver}/{dir}/{file}",
        "https://cdn.jsdelivr.net/npm/{pkg}@{ver}/{dir}/{file}")
_lock = threading.Lock()


def allowed(key: str, fname: str) -> bool:
    return key in PACKAGES and fname in PACKAGES[key][3]


def path(key: str, fname: str) -> Path:
    """回傳本機快取的檔案;沒有就先下載。只接受清單上的檔案。"""
    if not allowed(key, fname):
        raise FileNotFoundError(fname)
    dest = CACHE / key / fname
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    with _lock:
        if dest.is_file() and dest.stat().st_size > 0:
            return dest
        pkg, ver, d, _files = PACKAGES[key]
        dest.parent.mkdir(parents=True, exist_ok=True)
        spike = SPIKE_CACHE / key / fname          # 第 0 階段實測頁下載過的,直接沿用
        if spike.is_file() and spike.stat().st_size > 0:
            shutil.copyfile(spike, dest)
            return dest
        last: Exception | None = None
        for tpl in CDNS:
            url = tpl.format(pkg=pkg, ver=ver, dir=d, file=fname)
            tmp = dest.with_name(dest.name + ".part")
            try:
                with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
                    shutil.copyfileobj(r, f, 1 << 20)
                tmp.replace(dest)
                return dest
            except Exception as e:  # noqa: BLE001
                last = e
                tmp.unlink(missing_ok=True)
        raise RuntimeError(f"無法下載瀏覽器抽音訊元件 {pkg}/{fname}:{last}")


def main() -> int:
    """預先下載全部元件(伺服器部署時用)。"""
    from app.paths import setup_env

    setup_env()
    for key, (_pkg, _ver, _d, files) in PACKAGES.items():
        for f in files:
            p = path(key, f)
            print(f"OK  {p}  ({p.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
