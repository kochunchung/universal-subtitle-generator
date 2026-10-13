"""API 金鑰存在 Windows 認證管理員(keyring),不寫進設定檔。"""
from __future__ import annotations

SERVICE = "subtitle-generator"
PROVIDERS = ("anthropic", "openai")


def get_key(provider: str) -> str | None:
    import keyring

    try:
        return keyring.get_password(SERVICE, provider)
    except Exception:
        return None


def set_key(provider: str, key: str) -> None:
    import keyring

    keyring.set_password(SERVICE, provider, key.strip())


def delete_key(provider: str) -> None:
    import keyring

    try:
        keyring.delete_password(SERVICE, provider)
    except Exception:
        pass


def has_key(provider: str) -> bool:
    return bool(get_key(provider))
