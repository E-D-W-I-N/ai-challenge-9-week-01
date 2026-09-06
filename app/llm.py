"""Стриминг OpenRouter + сбор метрик.

Стриминг обязателен: без него нет ни TTFT, ни живого счётчика скорости.

Общее правило для всех вызовов — provider.require_parameters = true.
Без него OpenRouter вправе увести запрос к провайдеру, который молча
проигнорирует temperature или stop, и день покажет неправду.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import AsyncIterator

import httpx

from .config import OPENROUTER_BASE_URL, api_key, attribution_headers
from .schema import Session

_SPEED_WINDOW_SECONDS = 5.0


@dataclass
class Metrics:
    """Живая статистика одного прогона одной сессии."""

    ttft_ms: float | None = None
    elapsed_ms: float = 0.0
    tokens_out: int = 0
    tokens_per_second: float = 0.0

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None

    finish_reason: str | None = None
    model: str | None = None
    provider: str | None = None
    context_length: int | None = None
    context_fill_pct: float | None = None

    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "ttft_ms": round(self.ttft_ms, 1) if self.ttft_ms is not None else None,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "tokens_out": self.tokens_out,
            "tokens_per_second": round(self.tokens_per_second, 2),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cost_usd": self.cost_usd,
            "finish_reason": self.finish_reason,
            "model": self.model,
            "provider": self.provider,
            "context_length": self.context_length,
            "context_fill_pct": (
                round(self.context_fill_pct, 2) if self.context_fill_pct is not None else None
            ),
            "error": self.error,
        }


@dataclass
class _SpeedTracker:
    """Скользящее окно по чанкам: (время, накопленные токены)."""

    points: deque = field(default_factory=lambda: deque(maxlen=512))

    def add(self, now: float, tokens: int) -> float:
        self.points.append((now, tokens))
        while len(self.points) > 2 and now - self.points[0][0] > _SPEED_WINDOW_SECONDS:
            self.points.popleft()
        if len(self.points) < 2:
            return 0.0
        t0, n0 = self.points[0]
        t1, n1 = self.points[-1]
        span = t1 - t0
        return (n1 - n0) / span if span > 0 else 0.0


def build_payload(session: Session, prompt_override: list[dict] | None = None) -> dict:
    """Тело запроса к OpenRouter. require_parameters — на каждом вызове."""
    payload: dict = {
        "model": session.model,
        "messages": prompt_override if prompt_override is not None else session.messages,
        "stream": True,
        # Просим OpenRouter вернуть usage в финальном чанке: cost и reasoning_tokens
        "usage": {"include": True},
        "provider": {"require_parameters": True},
    }
    if session.temperature is not None:
        payload["temperature"] = session.temperature
    if session.max_tokens is not None:
        payload["max_tokens"] = session.max_tokens
    if session.stop:
        payload["stop"] = session.stop
    if session.response_format is not None:
        payload["response_format"] = session.response_format

    for key, value in (session.extra_body or {}).items():
        if key == "provider" and isinstance(value, dict):
            payload["provider"] = {**payload["provider"], **value}
        else:
            payload[key] = value
    return payload


class MissingKeyError(RuntimeError):
    pass


async def stream_completion(
    session: Session,
    *,
    prompt_override: list[dict] | None = None,
    context_length: int | None = None,
) -> AsyncIterator[dict]:
    """Отдаёт события: {"type": "delta"|"metrics"|"done"|"error", ...}.

    Метрики обновляются по мере генерации, финальный usage приходит последним чанком.
    """
    key = api_key()
    if key is None:
        raise MissingKeyError(
            "OPENROUTER_API_KEY не найден. Скопируйте .env.example в .env и впишите ключ."
        )

    payload = build_payload(session, prompt_override)
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        **attribution_headers(),
    }

    metrics = Metrics(model=session.model, context_length=context_length or None)
    speed = _SpeedTracker()
    started = time.monotonic()
    text_parts: list[str] = []

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=20.0)) as client:
            async with client.stream(
                "POST",
                f"{OPENROUTER_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    metrics.error = f"HTTP {response.status_code}: {body[:600]}"
                    metrics.elapsed_ms = (time.monotonic() - started) * 1000
                    yield {"type": "error", "message": metrics.error, "metrics": metrics.as_dict()}
                    return

                async for line in response.aiter_lines():
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    now = time.monotonic()
                    metrics.elapsed_ms = (now - started) * 1000

                    if chunk.get("provider"):
                        metrics.provider = chunk["provider"]
                    if chunk.get("model"):
                        metrics.model = chunk["model"]

                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        piece = delta.get("content") or ""
                        if piece:
                            if metrics.ttft_ms is None:
                                metrics.ttft_ms = (now - started) * 1000
                            text_parts.append(piece)
                            # оценка «на глаз», пока не пришёл usage: ~4 символа на токен
                            metrics.tokens_out = max(
                                metrics.tokens_out + 1, len("".join(text_parts)) // 4
                            )
                            metrics.tokens_per_second = speed.add(now, metrics.tokens_out)
                            yield {
                                "type": "delta",
                                "text": piece,
                                "metrics": metrics.as_dict(),
                            }
                        if choice.get("finish_reason"):
                            metrics.finish_reason = choice["finish_reason"]

                    usage = chunk.get("usage")
                    if usage:
                        _apply_usage(metrics, usage)
                        yield {"type": "metrics", "metrics": metrics.as_dict()}

    except httpx.HTTPError as exc:
        metrics.error = f"{type(exc).__name__}: {exc}"
        metrics.elapsed_ms = (time.monotonic() - started) * 1000
        yield {"type": "error", "message": metrics.error, "metrics": metrics.as_dict()}
        return

    metrics.elapsed_ms = (time.monotonic() - started) * 1000
    yield {"type": "done", "text": "".join(text_parts), "metrics": metrics.as_dict()}


def _apply_usage(metrics: Metrics, usage: dict) -> None:
    metrics.prompt_tokens = usage.get("prompt_tokens")
    metrics.completion_tokens = usage.get("completion_tokens")
    metrics.total_tokens = usage.get("total_tokens")
    if metrics.completion_tokens:
        metrics.tokens_out = int(metrics.completion_tokens)

    details = usage.get("completion_tokens_details") or {}
    metrics.reasoning_tokens = details.get("reasoning_tokens")

    cost = usage.get("cost")
    if cost is not None:
        try:
            metrics.cost_usd = round(float(cost), 8)
        except (TypeError, ValueError):
            metrics.cost_usd = None
    # usage.cost_details.upstream_inference_cost равен 0 без BYOK — намеренно не берём.

    if metrics.context_length and metrics.total_tokens:
        metrics.context_fill_pct = 100.0 * metrics.total_tokens / metrics.context_length
