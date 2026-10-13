"""測試用的假 worker:行為與 app.jobs.worker.run 的訊息格式相同,但不載入任何模型。

環境變數(測試設定,子程序會繼承):
  FAKE_STEPS   每個檔案回報幾次進度(預設 5)
  FAKE_SLEEP   每次進度之間睡幾秒(預設 0.05)
  FAKE_CHILD   檔案路徑;設定時會啟動一個長時間執行的子程序(模擬 ffmpeg),並把它的 pid 寫進該檔案
  FAKE_CRASH   設為 1 時,處理第一個檔案後直接結束程序(模擬工作程序當掉)
"""
import os
import subprocess
import sys
import time
from pathlib import Path


def run(jobs, settings, q):
    steps = int(os.environ.get("FAKE_STEPS", "5"))
    nap = float(os.environ.get("FAKE_SLEEP", "0.05"))
    child_file = os.environ.get("FAKE_CHILD")
    if child_file:
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        Path(child_file).write_text(str(p.pid))
    tgt = settings.get("translate_to")
    g = os.environ.get("APP_GLOSSARY_FILE", "")
    q.put(("log", "glossary-file=" + g.replace("\\", "/").split("/users/")[-1]))
    q.put(("log", f"cjk={settings['segmenter']['max_chars_cjk']}"))
    eng = settings.get("translation_engine")
    if tgt and eng in ("anthropic", "openai"):          # 第 5 階段:每人的金鑰由環境變數帶進來(不記金鑰本身)
        k = os.environ.get(f"APP_API_KEY_{eng.upper()}")
        q.put(("log", f"api-key={'set' if k else 'none'}"))
        if eng == "openai":                               # 用真的核心 ApiTranslator 確認它讀得到(不連網路)
            from app.translate.api import ApiTranslator
            t = ApiTranslator("openai", settings)
            q.put(("log", f"api-client model={t.model} from-env={t.client.api_key == k} host={t.client.base_url.host}"))
    for j in jobs:
        idx, path = j["idx"], Path(j["path"])
        if j["kind"] == "media":
            q.put(("file", idx, "running", 0.0, "開始處理"))
            for i in range(steps):
                time.sleep(nap)
                q.put(("file", idx, "running", (i + 1) / steps * (0.8 if tgt else 1.0), f"轉錄 {i + 1}/{steps}"))
            srt = path.with_suffix("").with_name(path.stem + ".zh-TW.srt")
            srt.write_text("1\r\n00:00:00,000 --> 00:00:01,000\r\n你好\r\n", encoding="utf-8-sig")
            q.put(("outputs", idx, {"srt": str(srt), "mp3": str(path.with_suffix(".mp3"))}))
            q.put(("log", f"[{path.name}] 已轉錄(假的)"))
            if os.environ.get("FAKE_CRASH") == "1":
                os._exit(3)
            if os.environ.get("FAKE_RAISE") == "1":     # 模擬 worker 捕捉到例外:記錄錯誤後仍送出 done
                from app.paths import ROOT
                q.put(("file", idx, "failed", None, "RuntimeError:CUDA error: CUBLAS_STATUS_EXECUTION_FAILED"))
                q.put(("log", "工作程序發生錯誤:\nTraceback (most recent call last):\n"
                              f'  File "{ROOT}/app/jobs/worker.py", line 124, in _run\n'
                              "    transcriber.unload()\n"
                              "RuntimeError: CUDA error: device-side assert triggered\n"
                              "CUDA kernel errors might be asynchronously reported at some other API call\n"))
                q.put(("done",))
                return
            if tgt:
                q.put(("file", idx, "transcribed", 0.8, "已轉錄,等待翻譯"))
            else:
                q.put(("file", idx, "done", 1.0, "完成"))
        else:
            q.put(("file", idx, "running", 0.0, "翻譯中"))
            time.sleep(nap)
            out = path.with_suffix("").with_name(path.stem.split(".")[0] + f".{tgt}.srt")
            out.write_text("1\r\n00:00:00,000 --> 00:00:01,000\r\nhello\r\n", encoding="utf-8-sig")
            q.put(("outputs", idx, {"translated": str(out)}))
            q.put(("file", idx, "done", 1.0, "完成"))
    if tgt:   # 媒體工作轉錄完之後的翻譯(與真正 worker 的順序一致)
        for j in jobs:
            if j["kind"] == "media":
                idx = j["idx"]
                srt = Path(j["path"]).with_suffix("").with_name(Path(j["path"]).stem + ".zh-TW.srt")
                out = srt.with_name(Path(j["path"]).stem + f".{tgt}.srt")
                q.put(("file", idx, "running", 0.8, "翻譯中"))
                out.write_text("1\r\n00:00:00,000 --> 00:00:01,000\r\nhello\r\n", encoding="utf-8-sig")
                q.put(("outputs", idx, {"translated": str(out)}))
                q.put(("file", idx, "done", 1.0, "完成"))
    q.put(("done",))
