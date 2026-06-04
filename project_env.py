"""Nạp biến từ .env vào os.environ (không ghi đè biến đã export sẵn)."""

from __future__ import annotations

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_LOADED = False


def load_project_env(env_path: Path | None = None) -> None:
    global _LOADED
    if _LOADED:
        return
    path = env_path or (_ROOT / ".env")
    if not path.is_file():
        _LOADED = True
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        val = val.strip().strip('"').strip("'")
        os.environ[key] = val
    _LOADED = True


def get_env(key: str, default: str = "") -> str:
    load_project_env()
    return os.environ.get(key, default).strip()
