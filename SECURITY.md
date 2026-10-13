# 安全性說明

## 回報漏洞

請不要公開張貼。請用 GitHub 的 **Security → Report a vulnerability** 私下回報。

## 架設網頁版前請確認

1. `WEB_DEV_LOGIN=0`。開發登入讓任何人輸入名稱就能登入，只能在自己的電腦測試。
2. 設定 `WEB_SECRET_KEY`（簽署登入 Cookie）與 `WEB_KEY_SECRET`（加密使用者的 API 金鑰），
   各用 48 字元以上的隨機字串。遺失 `WEB_KEY_SECRET` 後，所有人要重新填金鑰。
3. 對外服務一定要 HTTPS，`WEB_PUBLIC_URL` 用 `https://`（會自動開啟 Cookie 的 Secure）。
4. 預設只監聽 `127.0.0.1`；要對外請放在 Nginx / Caddy 等反向代理後面，不要直接開放。
5. `.env`、`data/`、Google 的 `client_secret*.json` 不要放進版本控制或備份到公開位置。
6. `WEB_REQUIRE_APPROVAL=1`（新帳號需管理者核准），並用 `WEB_ADMIN_EMAILS` 指定管理者。
7. 對外服務前請閱讀你所在地區對個資與使用者上傳內容的法規（影片可能含個資）。

## 已內建的防護

- 登入 Cookie：伺服器簽署、HttpOnly、SameSite=Lax；寫入類請求需 CSRF 權杖。
- Google 登入使用 OpenID Connect，驗證 ID token 簽章、對象、有效期與 nonce，且信箱必須已驗證。
- 使用者的 Claude / OpenAI 金鑰以 Fernet 加密保存，畫面只顯示末四碼；只在工作開始時解密，以環境變數交給處理程序，不寫入磁碟。
- 每位使用者的檔案分開存放，並依會員等級限制大小、時數、空間與排隊數。
- 上傳檔與字幕依保存天數自動清除；使用者可一鍵刪除自己的所有資料。
