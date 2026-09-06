"""Каталог моделей OpenRouter: /api/v1/models.

714 КБ и ~431 запись, ключа НЕ требует — дропдаун живой ещё до того, как
появится .env. Кэшируется в процессе с TTL.
"""

from __future__ import annotations

import time

import httpx

from .config import OPENROUTER_BASE_URL

# Ручного сброса кэша нет намеренно: кнопки в UI не было, а каталог сам
# обновится по TTL. Понадобится — вернём вместе с кнопкой, а не отдельным
# параметром, который некому нажать.
_TTL_SECONDS = 15 * 60
_cache: dict[str, object] = {"fetched_at": 0.0, "models": []}

# День 4: anthropic/* обрезает температуру на 1.0 и вернёт 400 на temperature=1.2,
# при этом честно перечисляет "temperature" в supported_parameters — фильтр их пропустит.
# Поэтому исключаем отдельно.
TEMPERATURE_CAPPED_PREFIXES = ("anthropic/",)

# День 5: :free всегда стоит 0 (сравнивать нечего), :batch — другая семантика задержки.
EXCLUDED_SUFFIXES = (":free", ":batch")


async def fetch_models() -> list[dict]:
    now = time.monotonic()
    if _cache["models"] and now - float(_cache["fetched_at"]) < _TTL_SECONDS:
        return list(_cache["models"])  # type: ignore[arg-type]

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{OPENROUTER_BASE_URL}/models")
        response.raise_for_status()
        payload = response.json()

    models = [_normalize(m) for m in payload.get("data", [])]
    models.sort(key=lambda m: m["id"])
    _cache["models"] = models
    _cache["fetched_at"] = now
    return list(models)


def _price(pricing: dict, key: str) -> float:
    try:
        return float(pricing.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _normalize(raw: dict) -> dict:
    pricing = raw.get("pricing") or {}
    model_id = raw.get("id", "")
    prompt_price = _price(pricing, "prompt")
    completion_price = _price(pricing, "completion")
    supported = raw.get("supported_parameters") or []
    return {
        "id": model_id,
        "name": raw.get("name") or model_id,
        "context_length": raw.get("context_length") or 0,
        "supported_parameters": supported,
        # цены за 1M токенов — то, в чём их привычно читать
        "prompt_price_per_m": round(prompt_price * 1_000_000, 4),
        "completion_price_per_m": round(completion_price * 1_000_000, 4),
        "is_free": model_id.endswith(":free") or (prompt_price == 0 and completion_price == 0),
        "temperature_capped": model_id.startswith(TEMPERATURE_CAPPED_PREFIXES),
    }


def filter_models(
    models: list[dict],
    *,
    requires: tuple[str, ...] = (),
    exclude_free: bool = False,
    exclude_temperature_capped: bool = False,
) -> list[dict]:
    """Фильтры для дропдаунов UI.

    requires — параметры, которые модель обязана поддерживать ("temperature", "stop",
    "response_format", "max_tokens").
    """
    result = []
    for model in models:
        if any(model["id"].endswith(suffix) for suffix in EXCLUDED_SUFFIXES):
            continue
        if requires and not all(p in model["supported_parameters"] for p in requires):
            continue
        if exclude_free and model["is_free"]:
            continue
        if exclude_temperature_capped and model["temperature_capped"]:
            continue
        result.append(model)
    return result

