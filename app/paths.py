"""程式資料夾內的固定路徑,以及讓所有模組只使用這些路徑的環境設定。

任何會用到 Hugging Face 或 ffmpeg 的模組,都要在 import 那些套件之前呼叫 setup_env()。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON_DIR = ROOT / "python"
FFMPEG_DIR = ROOT / "ffmpeg"
MODELS_DIR = ROOT / "models"
CONFIG_DIR = ROOT / "config"
LOGS_DIR = ROOT / "logs"

_EXE = ".exe" if os.name == "nt" else ""


def setup_env() -> None:
    """強制使用程式資料夾內的模型快取與 ffmpeg,不受使用者全域設定影響。"""
    os.environ["HF_HOME"] = str(MODELS_DIR)
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    if FFMPEG_DIR.is_dir():
        path = os.environ.get("PATH", "")
        if str(FFMPEG_DIR) not in path.split(os.pathsep):
            os.environ["PATH"] = str(FFMPEG_DIR) + os.pathsep + path
    for d in (MODELS_DIR, CONFIG_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    _use_system_certificates()


def _use_system_certificates() -> None:
    """改用 Windows 的憑證存放區驗證 HTTPS。
    公司網路若有 SSL 檢查(防火牆替換憑證),Python 內建的憑證清單會驗證失敗,
    而公司憑證通常已安裝在 Windows 裡。"""
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        pass


def tool_path(name: str) -> str | None:
    """回傳 ffmpeg / ffprobe 的完整路徑;優先使用程式資料夾內的版本。"""
    local = FFMPEG_DIR / f"{name}{_EXE}"
    if local.is_file():
        return str(local)
    return shutil.which(name)
