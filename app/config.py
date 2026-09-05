"""Поиск ключа OpenRouter и настроек. Ключ никогда не логируется и не отдаётся в API."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _load_dotenv() -> None:
    """Читает .env в корне репозитория, не перетирая уже заданные переменные."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def api_key() -> str | None:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    return key or None


def has_key() -> bool:
    return api_key() is not None


def attribution_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    site = os.environ.get("OPENROUTER_SITE_URL", "").strip()
    name = os.environ.get("OPENROUTER_SITE_NAME", "").strip()
    if site:
        headers["HTTP-Referer"] = site
    if name:
        headers["X-Title"] = name
    return headers
