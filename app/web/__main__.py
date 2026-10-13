"""啟動網頁版:python -m app.web

開發時同時啟動網站與 GPU 工作執行器(同一個程序裡的執行緒)。
正式環境若要把 GPU 放在另一台機器:網站加 --no-runner,GPU 機器執行 python -m app.web.runner。
埠號只看 .env 的 WEB_PORT(沒填 = 8780);--open 會在網站啟動後自動開瀏覽器。
"""
from __future__ import annotations

import argparse
import errno
import logging
import multiprocessing as mp
import socket
import sys
import threading
import time
import webbrowser


def port_in_use(host: str, port: int) -> bool:
    """這個埠號已經有程式在用了嗎?先試著連線(有人在聽),再試著佔用(Windows 上兩種情況都要檢查)。"""
    probe = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    try:
        with socket.create_connection((probe, port), timeout=0.5):
            return True
    except OSError:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):           # Windows:不允許和別的程式共用埠號
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:        # Linux/macOS:剛關掉的網站留下的 TIME_WAIT 不算佔用(uvicorn 自己也是這樣開)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        return False
    except OSError as e:
        return e.errno in (errno.EADDRINUSE, errno.EACCES, getattr(errno, "WSAEADDRINUSE", -1), 10013, 10048)
    finally:
        s.close()


def browser_url(public_url: str, port: int) -> str:
    """要開的網址:WEB_PUBLIC_URL 是本機網址時用它(Google 登入要同一個網址),否則用 localhost。"""
    if public_url.startswith(("http://localhost", "http://127.0.0.1")) and url_port(public_url) == port:
        return public_url + "/"
    return f"http://localhost:{port}/"


def url_port(url: str) -> int | None:
    from urllib.parse import urlsplit

    try:
        u = urlsplit(url)
        return u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        return None


def open_when_ready(url: str, host: str, port: int, timeout: float = 60) -> None:
    def run() -> None:
        probe = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
        t = time.monotonic()
        while time.monotonic() - t < timeout:
            try:
                with socket.create_connection((probe, port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.3)
        webbrowser.open(url)

    threading.Thread(target=run, name="open-browser", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app.web")
    ap.add_argument("--no-runner", action="store_true", help="只啟動網站,不啟動 GPU 工作執行器")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--open", action="store_true", help="網站啟動後自動用瀏覽器開啟")
    args = ap.parse_args(argv)

    from app.paths import LOGS_DIR, setup_env

    setup_env()
    logging.basicConfig(filename=LOGS_DIR / "web.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from app.web import config as config_mod
    from app.web.api import create_app

    cfg = config_mod.load()
    host, port = args.host or cfg.host, args.port or cfg.port
    if port_in_use(host, port):
        print(f"\n{port} 已被其他程式使用,請在 .env 改 WEB_PORT(例如 WEB_PORT=8781),"
              "並把 WEB_PUBLIC_URL 改成同一個埠號。")
        print("改了埠號後,Google 登入的「已授權的重新導向 URI」也要加上新網址(見「Google 登入申請步驟」)。")
        return 2
    local = cfg.public_url.startswith(("http://localhost", "http://127.0.0.1"))
    if local and url_port(cfg.public_url) != port:
        print(f"注意:.env 的 WEB_PUBLIC_URL({cfg.public_url})和 WEB_PORT({port})埠號不同,"
              "Google 登入會失敗。請改成一樣的埠號。")
    app = create_app(cfg, with_runner=not args.no_runner)
    url = browser_url(cfg.public_url, port)
    print(f"網頁版已啟動:{url}" + ("  (開發登入已開啟)" if cfg.dev_login else ""))
    if not args.no_runner:
        print("GPU 工作執行器已在這個程序內啟動。按 Ctrl+C 結束。")
    if args.open:
        open_when_ready(url, host, port)
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
