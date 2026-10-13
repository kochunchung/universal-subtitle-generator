"""使用者設定(config/settings.json)與模型清單。

API 金鑰不存在這裡,而是由 keyring 存在 Windows 認證管理員(第 2 階段實作)。
"""
from __future__ import annotations

import json
from typing import Any

from app.paths import CONFIG_DIR, MODELS_DIR

SETTINGS_FILE = CONFIG_DIR / "settings.json"

# 介面上的名稱 -> Hugging Face 模型代號
WHISPER_MODELS: dict[str, str] = {
    "large-v3-turbo(預設)": "openai/whisper-large-v3-turbo",
    "large-v3(較準、較慢)": "openai/whisper-large-v3",
    "medium(省顯存)": "openai/whisper-medium",
}
DEFAULT_WHISPER = "openai/whisper-large-v3-turbo"

TRANSLATION_ENGINES = {
    "m2m100": "離線 M2M100 1.2B(MIT 授權)",
    "nllb": "離線 NLLB-200 1.3B(CC-BY-NC 授權)",
    "anthropic": "Claude API",
    "openai": "OpenAI 相容 API",
}
OFFLINE_MODELS = {"m2m100": "facebook/m2m100_1.2B", "nllb": "facebook/nllb-200-distilled-1.3B"}

TRANSLATION_MODELS: dict[str, str] = {
    "M2M100 1.2B(預設,MIT 授權)": "facebook/m2m100_1.2B",
    "NLLB-200 1.3B(CC-BY-NC 授權)": "facebook/nllb-200-distilled-1.3B",
}

DEFAULTS: dict[str, Any] = {
    "whisper_model": DEFAULT_WHISPER,
    "source_language": "auto",
    "translate_to": None,            # None / "zh-TW" / "en" / "ja"
    "subtitle_mode": "translated",   # "translated" 或 "bilingual"
    "translation_engine": "m2m100",  # "m2m100" / "nllb" / "anthropic" / "openai"
    "offline_merge": False,          # 離線翻譯是否合併跨條句子(實測 NLLB 逐條較完整,預設關閉)
    "opencc_s2twp": True,
    "vad": True,
    "delete_mp3": False,
    "write_txt": False,
    "overwrite": False,
    "batch_size": None,              # None = 依顯存自動決定
    "calibration": {},               # 第 1 階段:各模型每筆批次的實測顯存用量
    "segmenter": {
        "max_chars_cjk": 18,
        "max_chars_latin": 42,
        "max_lines": 2,
        "max_duration": 7.0,
    },
    "api": {
        "anthropic_model": "",
        "openai_base_url": "",
        "openai_model": "",
    },
}


# 各模型需要的檔案。手動下載時,把「local」列出的檔案放在 models\<模型名稱>\
_WHISPER_FILES = {
    "allow": ["*.json", "*.txt", "*.safetensors", "*.model", "*.tiktoken"],
    "weights": ["model.safetensors"],
    "local": [
        "config.json", "generation_config.json", "model.safetensors", "preprocessor_config.json",
        "tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt", "normalizer.json",
        "added_tokens.json", "special_tokens_map.json",
    ],
}
_M2M_FILES = {
    "allow": ["*.json", "*.model", "pytorch_model.bin"],   # 不下載 rust_model.ot(與 PyTorch 無關,同樣 5 GB)
    "weights": ["pytorch_model.bin"],
    "local": [
        "config.json", "generation_config.json", "pytorch_model.bin", "sentencepiece.bpe.model",
        "special_tokens_map.json", "tokenizer_config.json", "vocab.json",
    ],
}
_NLLB_FILES = {
    "allow": ["*.json", "*.model", "pytorch_model.bin"],
    "weights": ["pytorch_model.bin"],
    "local": [
        "config.json", "generation_config.json", "pytorch_model.bin", "sentencepiece.bpe.model",
        "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json",
    ],
}
MODEL_FILES: dict[str, dict] = {
    "facebook/m2m100_1.2B": _M2M_FILES,
    "facebook/m2m100_418M": _M2M_FILES,
    "facebook/nllb-200-distilled-1.3B": _NLLB_FILES,
    "facebook/nllb-200-distilled-600M": _NLLB_FILES,
}


def model_files(repo_id: str) -> dict:
    return MODEL_FILES.get(repo_id, _WHISPER_FILES)


# 舊名稱相容(第 0、1 階段使用)
LOCAL_MODEL_FILES = _WHISPER_FILES["local"]


def local_model_dir(repo_id: str):
    """手動下載的模型資料夾位置:models/<模型名稱>"""
    return MODELS_DIR / repo_id.split("/")[-1]


def resolve_model(repo_id: str) -> str:
    """有完整的手動下載資料夾就用它,否則用 Hugging Face 快取中的模型代號。"""
    local = local_model_dir(repo_id)
    spec = model_files(repo_id)
    if (local / "config.json").is_file() and all((local / w).is_file() for w in spec["weights"]):
        return str(local)
    return repo_id


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict[str, Any]:
    if SETTINGS_FILE.is_file():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            return _merge(DEFAULTS, data)
        except (OSError, json.JSONDecodeError):
            pass
    return json.loads(json.dumps(DEFAULTS))


def save(settings: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)
