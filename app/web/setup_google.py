"""設定 Google 登入:把 Client ID / Client Secret 寫進程式資料夾的 .env。

  python -m app.web.setup_google

- .env 裡其他設定原樣保留,只更新 WEB_PORT、WEB_PUBLIC_URL、WEB_GOOGLE_CLIENT_ID、WEB_GOOGLE_CLIENT_SECRET、
  WEB_ADMIN_EMAILS、WEB_REQUIRE_APPROVAL。
- 還沒有 WEB_KEY_SECRET(加密使用者 API 金鑰的主金鑰)也自動產生一把。
- 還沒有 WEB_SECRET_KEY(登入 Cookie 的簽署金鑰)就自動產生一把。
- 金鑰不會顯示在畫面上,也不會寫進日誌。
"""
from __future__ import annotations

import getpass
import secrets
import sys
from pathlib import Path

from app.paths import ROOT
from app.web.config import DEFAULT_PORT

ENV = ROOT / ".env"


def read_env(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []


def set_values(lines: list[str], values: dict[str, str]) -> list[str]:
    """更新或新增 KEY=VALUE;其他行(含註解)不動。"""
    out, done = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in values:
            out.append(f"{key}={values[key]}")
            done.add(key)
        else:
            out.append(line)
    for k, v in values.items():
        if k not in done:
            out.append(f"{k}={v}")
    return out


def current(lines: list[str], key: str) -> str:
    for line in lines:
        if line.strip().startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


def main() -> int:
    print("=== 設定 Google 登入 ===\n")
    print("請先照說明在 Google Cloud 建立「網頁應用程式」的 OAuth 用戶端,再把資料貼到這裡。")
    print("(在這個視窗按滑鼠右鍵就是貼上)\n")
    lines = read_env(ENV)
    try:
        old_port = int(current(lines, "WEB_PORT") or DEFAULT_PORT)
    except ValueError:
        old_port = DEFAULT_PORT
    ans = input(f"網站埠號(port)[直接按 Enter 使用 {old_port}]:").strip() or str(old_port)
    if not ans.isdigit() or not 1024 <= int(ans) <= 65535:
        print("\n埠號要是 1024~65535 的數字,請再執行一次。")
        return 1
    port = int(ans)
    old_url = current(lines, "WEB_PUBLIC_URL")
    if not old_url or old_url.startswith(("http://localhost", "http://127.0.0.1")):
        old_url = f"http://localhost:{port}"          # 本機測試:網址跟著埠號走

    cid = input("Client ID(用戶端 ID,結尾是 .apps.googleusercontent.com):").strip()
    if not cid.endswith(".apps.googleusercontent.com"):
        print("\n這看起來不像 Google 的 Client ID,請確認後再執行一次。")
        return 1
    secret = getpass.getpass("Client Secret(用戶端密鑰,輸入時畫面不會顯示):").strip()
    if len(secret) < 10:
        print("\n用戶端密鑰太短,請確認後再執行一次。")
        return 1
    url = input(f"網站網址 [直接按 Enter 使用 {old_url}]:").strip().rstrip("/") or old_url
    if not url.startswith(("http://localhost", "http://127.0.0.1", "https://")):
        print("\nGoogle 只接受 https:// 網址(在自己電腦測試時可以用 http://localhost)。")
        return 1

    old_admin = current(lines, "WEB_ADMIN_EMAILS")
    hint = f"直接按 Enter 沿用 {old_admin}" if old_admin else "多個用逗號分隔"
    admins = input(f"管理者的 Google 信箱(新帳號要由管理者核准;{hint}):").strip() or old_admin
    if not admins or "@" not in admins:
        print("\n至少要指定一個管理者信箱,否則沒有人能核准新帳號。")
        return 1

    values = {"WEB_PORT": str(port), "WEB_GOOGLE_CLIENT_ID": cid, "WEB_GOOGLE_CLIENT_SECRET": secret, "WEB_PUBLIC_URL": url,
              "WEB_ADMIN_EMAILS": admins.replace(" ", ""), "WEB_REQUIRE_APPROVAL": "1"}
    if not current(lines, "WEB_SECRET_KEY"):
        values["WEB_SECRET_KEY"] = secrets.token_urlsafe(48)
    if not current(lines, "WEB_KEY_SECRET"):          # 加密使用者 API 金鑰的主金鑰;換掉後大家要重填金鑰
        values["WEB_KEY_SECRET"] = secrets.token_urlsafe(48)
    ENV.write_text("\n".join(set_values(lines, values)) + "\n", encoding="utf-8")

    print(f"\n已寫入 {ENV}")
    print("\n請確認 Google Cloud 的 OAuth 用戶端裡,「已授權的重新導向 URI」有這一行(要一模一樣):")
    print(f"    {url}/auth/google/callback")
    print("(以後改了埠號或網址,要回到 Google Cloud 再加一行新的;舊的那行可以留著)")
    print("\n設定完成後,重新啟動網頁版就會出現「使用 Google 帳號登入」按鈕。")
    print("新帳號第一次登入後會停在「等待核准」,管理者到網頁上方的「管理」頁按「核准」即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
