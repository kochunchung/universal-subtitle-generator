# 萬能字幕產生器 · Universal Subtitle Generator

**把影片拖進來，幾分鐘後拿到字幕（SRT）— 還能直接翻成你要的語言。**
語音辨識在你自己的 NVIDIA 顯示卡上執行，影片不用上傳給第三方；6 GB 顯存就跑得動。

> Drag in a video, get an SRT subtitle file — and translate it. Speech recognition runs locally on your own NVIDIA GPU (6 GB VRAM is enough). Includes a Windows desktop app and a multi-user web version.

<!-- 放一張截圖或 GIF：docs/screenshot.png -->
<!-- ![screenshot](docs/screenshot.png) -->

## 為什麼用它

- **本機辨識，資料不外流**：採用 Whisper large-v3-turbo，中文、英文、日文等多語言都能辨識。
- **快**：52 分鐘的會議錄影約 2 分鐘完成（RTX 3060 筆電實測，約 49 倍即時）。
- **6 GB 顯存就夠**：第一次用「顯存校準」自動找出最佳批次，不會爆顯存。
- **轉錄與翻譯分開**：先轉錄，之後想翻英文、日文再按一次，不會重跑辨識；一份影片可以有多種語言字幕。
- **翻譯引擎自選**：離線（M2M100）完全不用網路，或接 Claude / OpenAI 取得更自然的口語翻譯。
- **詞彙表**：專有名詞可提高辨識率，並固定譯法。
- **中文字幕排版**：自動斷句（每行字數、行數、時長可調），繁體輸出（OpenCC）。
- **整個資料夾可攜**：不需要系統管理員權限，複製到另一台 NVIDIA 電腦就能用。

## 兩種用法

| | 桌面版 | 網頁版 |
| --- | --- | --- |
| 適合 | 自己用、單機 | 讓同事／團隊用瀏覽器上傳 |
| 登入 | 不需要 | Google 登入，管理者核准帳號 |
| 會員等級與限額 | — | 一般／付費，管理頁可調 |
| API 金鑰 | 存在 Windows 認證管理員 | 每人自己的金鑰，加密保存 |
| 上傳 | 直接讀本機檔案 | 瀏覽器先抽出聲音再上傳（省流量），可續傳 |
| 檔案保存 | 在影片旁邊 | 到期自動清除 |

## 速度實測

在一台 RTX 3060 筆電（6 GB 顯存）上，用 Whisper large-v3-turbo（fp16）轉錄一場 4 GB 的中文會議錄影：

| 項目 | 數據 |
| --- | --- |
| 影片長度 | 52 分 26 秒 |
| 總耗時（含轉 MP3、偵測人聲、載入模型、轉錄） | 約 2 分 03 秒 |
| 轉錄速度 | 約 49 倍即時 |
| 顯存峰值 | 2.3 GB |
| 字幕 | 654 條，無幻聽、無漏段 |

實際速度會因顯示卡、影片內容與語言而不同。這是單一測試的結果，不是保證值。

## 系統需求

- Windows 10/11、NVIDIA 顯示卡（驅動 531.14 以上，建議 6 GB 以上顯存）
- 約 10 GB 磁碟空間（離線翻譯模型另外約 5 GB）

## 快速開始（桌面版）

1. 下載或 clone 這個專案。
2. 雙擊 `install.bat`（不需系統管理員權限）。
3. 雙擊 `calibrate-vram.bat`（約 1 分鐘，每種模型做一次）。
4. 雙擊 `start-desktop.bat`，把影片拖進視窗，按「開始處理」。

輸出檔放在影片旁邊：`影片名.zh-TW.srt`（原文）、`影片名.en.srt`（譯文）、`影片名.zh-TW-en.srt`（雙語）。

## 架設網頁版

```text
1. install-web.bat
2. 複製 .env.example 成 .env，或執行 setup-google-login.bat 依序填入
   （埠號、Google OAuth 用戶端 ID/密鑰、管理者信箱；簽署金鑰會自動產生）
3. start-web.bat   →  http://localhost:8780/
```

- Google 登入需要自己在 Google Cloud 建立 OAuth 用戶端（網頁應用程式），
  重新導向 URI 填 `你的網址/auth/google/callback`。
- 對外開放時請務必：使用 HTTPS、`WEB_DEV_LOGIN=0`、設定 `WEB_SECRET_KEY` 與 `WEB_KEY_SECRET`。
  詳見 [SECURITY.md](SECURITY.md)。

## 命令列工具

`check-env.bat`（檢查 CUDA、PyTorch、ffmpeg、模型）、`transcribe.bat`、`translate-srt.bat`、`set-api-key.bat`、`diag-decode.bat`、`diag-translate.bat`。

## 專案結構

```text
app/core        轉錄核心（Whisper、VAD、斷句、對齊）
app/translate   翻譯（離線與 API）
app/gui         桌面版（PySide6）
app/web         網頁版（FastAPI）：登入、權限、佇列、上傳、清除
tests/web       網頁版自動化測試
config          設定與詞彙表範例
```

## 授權與第三方元件

本專案程式碼以 MIT 授權釋出（見 [LICENSE](LICENSE)）。
模型與第三方元件各有自己的授權，請自行確認使用情境是否允許：

- Whisper large-v3-turbo（MIT）、Silero VAD（MIT）、M2M100（MIT）
- NLLB-200 為 CC-BY-NC 4.0（**僅限非商業**；網頁版預設不提供）
- FFmpeg 為 LGPL/GPL，本專案不內含執行檔，安裝腳本會另外下載
- PySide6 為 LGPL

## 狀態

網頁版仍在開發中。歡迎開 Issue 回報問題或建議。
