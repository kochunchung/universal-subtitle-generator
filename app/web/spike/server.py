"""網頁版第 0 階段:實測伺服器(只用標準函式庫,不需要安裝任何新套件)。

用途:驗證整個網頁版計畫裡唯一還沒實測過的部分
  A. 一般會員路線:瀏覽器用 ffmpeg.wasm 從影片抽出音訊,只上傳音訊
  B. 付費會員路線:原始大影片分塊上傳(可續傳),伺服器抽音訊

只綁定 127.0.0.1(本機),測試完資料立刻刪除。執行方式:啟動第0階段實測.bat
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.paths import ROOT, setup_env

setup_env()

from app.core import media  # noqa: E402  (需要先 setup_env 才找得到內建的 ffmpeg)

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
DATA = ROOT / "data" / "spike"
VENDOR_CACHE = DATA / "vendor"
UPLOADS = DATA / "uploads"

HOST, PORT = "127.0.0.1", 8765
CHUNK_MB = 16
READ_BLOCK = 1 << 20

# 規劃文件第 12 節的起始值(這裡只用來驗證「伺服器端強制檢查」的做法)
TIERS = {
    "free": {"label": "一般會員", "max_bytes": 200 * 2**20, "max_seconds": 3 * 3600,
             "exts": media.AUDIO_EXTS},
    "paid": {"label": "付費會員", "max_bytes": 6 * 2**30, "max_seconds": 5 * 3600,
             "exts": media.AUDIO_EXTS | media.VIDEO_EXTS},
}

# 前端用到的 ffmpeg.wasm 套件:本機路徑 → (套件, 版本, 套件內資料夾)。
# 第一次需要時從網路抓下來存在 data\spike\vendor,之後離線也能用。
VENDOR = {
    "ffmpeg": ("@ffmpeg/ffmpeg", "0.12.10", "dist/esm"),
    "util": ("@ffmpeg/util", "0.12.1", "dist/esm"),
    "core": ("@ffmpeg/core", "0.12.6", "dist/esm"),
}
VENDOR_FILES = {
    "ffmpeg": {"index.js", "classes.js", "const.js", "errors.js", "types.js", "utils.js", "worker.js"},
    "util": {"index.js", "const.js", "errors.js", "types.js"},
    "core": {"ffmpeg-core.js", "ffmpeg-core.wasm"},
}
CDNS = ("https://unpkg.com/{pkg}@{ver}/{dir}/{file}",
        "https://cdn.jsdelivr.net/npm/{pkg}@{ver}/{dir}/{file}")

UID_RE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
_vendor_lock = threading.Lock()


# ---------------------------------------------------------------------- 上傳檔案
def _suffix(name: str) -> str:
    """只取副檔名(小寫,限英數);檔名本身不拿來當路徑。"""
    ext = Path(name or "").suffix.lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{1,5}", ext) else ""


def _meta_path(uid: str) -> Path:
    return UPLOADS / f"{uid}.json"


def _load_meta(uid: str) -> dict | None:
    p = _meta_path(uid)
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _data_path(meta: dict) -> Path:
    return UPLOADS / f"{meta['uid']}{meta['ext']}"


def _received(uid: str) -> int:
    meta = _load_meta(uid)
    if not meta:
        return 0
    p = _data_path(meta)
    return p.stat().st_size if p.is_file() else 0


def _drop(uid: str) -> None:
    meta = _load_meta(uid)
    if meta:
        _data_path(meta).unlink(missing_ok=True)
    _meta_path(uid).unlink(missing_ok=True)
    (UPLOADS / f"{uid}.mp3").unlink(missing_ok=True)


def _cleanup_old(max_age_s: int = 6 * 3600) -> None:
    now = time.time()
    for p in UPLOADS.glob("*"):
        try:
            if now - p.stat().st_mtime > max_age_s:
                p.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------- 套件快取
def _fetch_vendor(key: str, fname: str) -> Path:
    pkg, ver, d = VENDOR[key]
    dest = VENDOR_CACHE / key / fname
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    with _vendor_lock:
        if dest.is_file() and dest.stat().st_size > 0:
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        last: Exception | None = None
        for tpl in CDNS:
            url = tpl.format(pkg=pkg, ver=ver, dir=d, file=fname)
            tmp = dest.with_name(dest.name + ".part")
            try:
                with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
                    shutil.copyfileobj(r, f, 1 << 20)
                tmp.replace(dest)
                print(f"[套件] 已下載 {url}")
                return dest
            except Exception as e:  # noqa: BLE001
                last = e
                tmp.unlink(missing_ok=True)
        raise RuntimeError(f"無法下載 {pkg} 的 {fname}:{last}")


# ---------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Spike/0"

    def log_message(self, fmt: str, *args) -> None:  # 安靜一點;上傳的每一塊都印會很吵
        if "/api/upload/" not in (args[0] if args else ""):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ------------------------------------------------------------ 共用
    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str, cache: str = "no-store") -> None:
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1 << 20)

    def _query(self) -> tuple[str, dict]:
        u = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        return u.path, q

    # ------------------------------------------------------------ GET
    def do_GET(self) -> None:  # noqa: N802
        path, _q = self._query()
        try:
            if path in ("/", "/index.html"):
                return self._file(STATIC / "index.html", "text/html; charset=utf-8")
            m = re.fullmatch(r"/vendor/(ffmpeg|util|core)/([A-Za-z0-9.\-]+)", path)
            if m:
                key, fname = m.groups()
                if fname not in VENDOR_FILES[key]:
                    return self._json(404, {"error": "no such file"})
                ctype = ("application/wasm" if fname.endswith(".wasm")
                         else "text/javascript; charset=utf-8")
                return self._file(_fetch_vendor(key, fname), ctype, "max-age=86400")
            if path == "/api/info":
                return self._json(200, {
                    "chunk_bytes": CHUNK_MB * 2**20,
                    "tiers": {k: {"label": v["label"], "max_bytes": v["max_bytes"],
                                  "max_seconds": v["max_seconds"]} for k, v in TIERS.items()},
                    "server_ffmpeg": bool(media.tool_path("ffmpeg")) if hasattr(media, "tool_path") else True,
                })
            m = re.fullmatch(r"/api/upload/([A-Za-z0-9_-]+)", path)
            if m and UID_RE.match(m.group(1)):
                return self._json(200, {"received": _received(m.group(1))})
        except Exception as e:  # noqa: BLE001
            return self._json(500, {"error": str(e)})
        self._json(404, {"error": "not found"})

    # ------------------------------------------------------------ PUT 分塊
    def do_PUT(self) -> None:  # noqa: N802
        path, q = self._query()
        m = re.fullmatch(r"/api/upload/([A-Za-z0-9_-]+)", path)
        if not (m and UID_RE.match(m.group(1))):
            return self._json(404, {"error": "not found"})
        uid = m.group(1)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            offset, total = int(q["offset"]), int(q["total"])
            tier = TIERS[q.get("tier", "free")]
        except (KeyError, ValueError):
            self._drain(length)
            return self._json(400, {"error": "參數錯誤"})

        # 上限一律在伺服器檢查:開始上傳時就擋,不等傳完
        ext = _suffix(q.get("name", ""))
        if total > tier["max_bytes"]:
            self._drain(length)
            return self._json(413, {"error": f"超過{tier['label']}的檔案大小上限"})
        if ext not in tier["exts"]:
            self._drain(length)
            return self._json(415, {"error": f"{tier['label']}不接受這種檔案類型({ext or '無副檔名'})"})
        if offset + length > total:
            self._drain(length)
            return self._json(400, {"error": "超出宣告的檔案大小"})

        UPLOADS.mkdir(parents=True, exist_ok=True)
        meta = _load_meta(uid)
        if meta is None:
            if offset != 0:
                self._drain(length)
                return self._json(409, {"error": "找不到這次上傳", "received": 0})
            meta = {"uid": uid, "ext": ext, "total": total, "tier": q.get("tier", "free")}
            _meta_path(uid).write_text(json.dumps(meta), encoding="utf-8")
        elif meta["total"] != total or meta["ext"] != ext:
            self._drain(length)
            return self._json(400, {"error": "與先前宣告的檔案不同"})

        dst = _data_path(meta)
        have = dst.stat().st_size if dst.is_file() else 0
        if offset != have:   # 位置對不上(例如重複送了同一塊):告訴前端伺服器實際收到哪裡
            self._drain(length)
            return self._json(409, {"error": "位置不符", "received": have})

        try:
            with open(dst, "ab") as f:
                left = length
                while left > 0:
                    block = self.rfile.read(min(READ_BLOCK, left))
                    if not block:
                        break
                    f.write(block)
                    left -= len(block)
        except (ConnectionError, OSError):
            return   # 連線中斷:已寫入的部分保留,前端續傳時會先問收到多少
        got = dst.stat().st_size
        self._json(200, {"received": got})

    def _drain(self, n: int) -> None:
        """被拒絕的請求也要把內容讀掉(或關閉連線),否則 keep-alive 連線會錯位。"""
        if n > 64 * 2**20:
            self.close_connection = True
            return
        while n > 0:
            b = self.rfile.read(min(READ_BLOCK, n))
            if not b:
                break
            n -= len(b)

    # ------------------------------------------------------------ POST 完成
    def do_POST(self) -> None:  # noqa: N802
        path, q = self._query()
        length = int(self.headers.get("Content-Length") or 0)
        self._drain(length)
        m = re.fullmatch(r"/api/finish/([A-Za-z0-9_-]+)", path)
        if m and UID_RE.match(m.group(1)):
            return self._finish(m.group(1), q)
        m = re.fullmatch(r"/api/discard/([A-Za-z0-9_-]+)", path)
        if m and UID_RE.match(m.group(1)):
            _drop(m.group(1))
            return self._json(200, {"ok": True})
        self._json(404, {"error": "not found"})

    def _finish(self, uid: str, q: dict) -> None:
        meta = _load_meta(uid)
        if not meta:
            return self._json(404, {"error": "找不到這次上傳"})
        tier = TIERS[meta["tier"]]
        src = _data_path(meta)
        size = src.stat().st_size if src.is_file() else 0
        if size != meta["total"]:
            return self._json(409, {"error": "檔案還沒收完", "received": size, "total": meta["total"]})
        out: dict = {"ok": True, "size": size, "tier": tier["label"]}
        try:
            t0 = time.perf_counter()
            info = media.probe(src)
            out["probe_s"] = round(time.perf_counter() - t0, 2)
            out["duration_s"] = round(info.duration, 2)
            out["audio_streams"] = [s.label() + f"({s.codec}, {s.channels}ch)" for s in info.audio_streams]
            if info.duration > tier["max_seconds"]:
                out.update(ok=False, error=f"長度超過{tier['label']}的上限({tier['max_seconds'] // 3600} 小時)")
            elif q.get("extract") == "1":
                dst = UPLOADS / f"{uid}.mp3"
                t1 = time.perf_counter()
                media.extract_mp3(src, dst, 0, info.duration)
                out["extract_s"] = round(time.perf_counter() - t1, 2)
                out["mp3_size"] = dst.stat().st_size
                out["speed_x"] = round(info.duration / max(out["extract_s"], 0.01), 1)
                dst.unlink(missing_ok=True)   # 規劃:抽完音訊立即刪除;這裡測完也刪
        except media.MediaError as e:
            # 錯誤訊息不能帶出伺服器上的路徑
            out.update(ok=False, error=str(e).replace(str(src), "檔案"))
        except Exception as e:  # noqa: BLE001
            out.update(ok=False, error=f"{e.__class__.__name__}: {e}")
        finally:
            _drop(uid)   # 實測頁:測完就刪,不留任何檔案
        self._json(200, out)


def main() -> int:
    UPLOADS.mkdir(parents=True, exist_ok=True)
    VENDOR_CACHE.mkdir(parents=True, exist_ok=True)
    _cleanup_old()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"第 0 階段實測伺服器已啟動:http://{HOST}:{PORT}/  (按 Ctrl+C 結束)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
