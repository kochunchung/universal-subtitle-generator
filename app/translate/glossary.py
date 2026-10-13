"""詞彙表 config/glossary.txt:轉錄提示詞與翻譯固定譯法共用。

格式:一行一個詞;「原詞=譯詞」表示翻譯時固定譯法;# 開頭為註解。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from app.paths import CONFIG_DIR

GLOSSARY_FILE = CONFIG_DIR / "glossary.txt"


@dataclass
class Glossary:
    terms: list[str] = field(default_factory=list)              # 所有原詞(轉錄提示用)
    translations: dict[str, str] = field(default_factory=dict)  # 原詞 -> 譯詞

    def prompt_text(self, max_chars: int = 120) -> str:
        out, n = [], 0
        for t in self.terms:
            if n + len(t) + 1 > max_chars:
                break
            out.append(t)
            n += len(t) + 1
        return "、".join(out)


def load(path: str | Path | None = None) -> Glossary:
    g = Glossary()
    # 網頁版用環境變數指定每個人自己的詞彙表;沒設定時和桌面版一樣用 config/glossary.txt
    p = Path(path or os.environ.get("APP_GLOSSARY_FILE") or GLOSSARY_FILE)
    if not p.is_file():
        return g
    for raw in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            src, dst = (s.strip() for s in line.split("=", 1))
            if src:
                g.terms.append(src)
                if dst:
                    g.translations[src] = dst
        else:
            g.terms.append(line)
    return g
