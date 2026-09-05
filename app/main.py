"""FastAPI-стенд: список сценариев, запуск, SSE-поток живой статистики.

Файл заморожен для воркеров дней. Сценарии подключаются автоматически
из day-*/scenario.py — трогать app/ ради нового дня не нужно.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import catalog, registry
from .config import has_key
from .llm import MissingKeyError, stream_completion
from .schema import Scenario, Session

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="AI Challenge Bench", version="1.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def _session_public(session: Session) -> dict:
    data = asdict(session)
    data["messages_preview"] = "\n\n".join(
        f"[{m.get('role', '?')}] {str(m.get('content', ''))[:400]}" for m in session.messages
    )
    return data


def _scenario_public(scenario: Scenario) -> dict:
    return {
        "id": scenario.id,
        "title": scenario.title,
        "description": scenario.description,
        "watch_for": scenario.watch_for,
        "layout": scenario.layout,
        "sessions": [_session_public(s) for s in scenario.sessions],
    }


@app.get("/api/scenarios")
async def list_scenarios() -> dict:
    scenarios = registry.discover()
    return {
        "has_key": has_key(),
        "scenarios": [_scenario_public(s) for s in scenarios],
        "errors": registry.errors(),
    }


@app.get("/api/models")
async def list_models(
    requires: str = Query("", description="csv: temperature,stop,response_format"),
    exclude_free: bool = False,
    exclude_temperature_capped: bool = False,
    q: str = "",
    refresh: bool = False,
) -> dict:
    try:
        models = await catalog.fetch_models(force=refresh)
    except Exception as exc:  # каталог недоступен — UI не должен падать
        raise HTTPException(status_code=502, detail=f"каталог моделей недоступен: {exc}") from exc
    needed = tuple(p.strip() for p in requires.split(",") if p.strip())
    filtered = catalog.filter_models(
        models,
        requires=needed,
        exclude_free=exclude_free,
        exclude_temperature_capped=exclude_temperature_capped,
        query=q,
    )
    return {"total": len(models), "count": len(filtered), "models": filtered}


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _substitute(messages: list[dict], value: str) -> list[dict]:
    out = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str) and "{{depends_on}}" in content:
            message = {**message, "content": content.replace("{{depends_on}}", value)}
        out.append(message)
    return out


async def _run_session(
    session: Session,
    context_lengths: dict[str, int],
    queue: asyncio.Queue,
    results: dict[str, str],
    ready: dict[str, asyncio.Event],
) -> None:
    label = session.label
    try:
        if session.depends_on:
            waiter = ready.get(session.depends_on)
            if waiter is None:
                await queue.put(
                    {
                        "event": "session_error",
                        "session": label,
                        "message": f"depends_on: сессии «{session.depends_on}» нет в сценарии",
                    }
                )
                return
            await queue.put({"event": "session_waiting", "session": label, "on": session.depends_on})
            await waiter.wait()

        messages = session.messages
        if session.depends_on:
            messages = _substitute(messages, results.get(session.depends_on, ""))

        await queue.put(
            {
                "event": "session_start",
                "session": label,
                "repeats": session.repeats,
                "resolved_prompt": "\n\n".join(
                    f"[{m.get('role', '?')}] {m.get('content', '')}" for m in messages
                ),
            }
        )

        last_text = ""
        for index in range(max(1, session.repeats)):
            await queue.put({"event": "repeat_start", "session": label, "repeat": index})
            text = ""
            async for chunk in stream_completion(
                session,
                prompt_override=messages,
                context_length=context_lengths.get(session.model),
            ):
                kind = chunk["type"]
                if kind == "delta":
                    text += chunk["text"]
                    await queue.put(
                        {
                            "event": "delta",
                            "session": label,
                            "repeat": index,
                            "text": chunk["text"],
                            "metrics": chunk["metrics"],
                        }
                    )
                elif kind == "metrics":
                    await queue.put(
                        {
                            "event": "metrics",
                            "session": label,
                            "repeat": index,
                            "metrics": chunk["metrics"],
                        }
                    )
                elif kind == "error":
                    await queue.put(
                        {
                            "event": "repeat_error",
                            "session": label,
                            "repeat": index,
                            "message": chunk["message"],
                            "metrics": chunk["metrics"],
                        }
                    )
                elif kind == "done":
                    await queue.put(
                        {
                            "event": "repeat_done",
                            "session": label,
                            "repeat": index,
                            "text": chunk["text"],
                            "metrics": chunk["metrics"],
                        }
                    )
            last_text = text

        results[label] = last_text
        await queue.put({"event": "session_done", "session": label})
    except MissingKeyError as exc:
        await queue.put({"event": "session_error", "session": label, "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 — колонка падает одна, прогон продолжается
        await queue.put(
            {"event": "session_error", "session": label, "message": f"{type(exc).__name__}: {exc}"}
        )
    finally:
        event = ready.get(label)
        if event is not None:
            event.set()


@app.get("/api/run/{scenario_id}")
async def run_scenario(scenario_id: str, overrides: str = "") -> StreamingResponse:
    scenario = registry.get(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail=f"сценарий {scenario_id} не найден")

    # overrides: {"<session label>": {"model": "...", "temperature": 0.7}}
    patch: dict = {}
    if overrides:
        try:
            patch = json.loads(overrides)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"overrides не JSON: {exc}") from exc

    sessions: list[Session] = []
    for session in scenario.sessions:
        fields = asdict(session)
        fields.update(patch.get(session.label, {}))
        sessions.append(Session(**fields))

    try:
        models = await catalog.fetch_models()
        context_lengths = {m["id"]: m["context_length"] for m in models}
    except Exception:
        context_lengths = {}

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()
        results: dict[str, str] = {}
        ready = {s.label: asyncio.Event() for s in sessions}
        started = time.monotonic()

        yield _sse(
            {
                "event": "run_start",
                "scenario": scenario.id,
                "title": scenario.title,
                "layout": scenario.layout,
                "sessions": [_session_public(s) for s in sessions],
            }
        )

        tasks = [
            asyncio.create_task(_run_session(s, context_lengths, queue, results, ready))
            for s in sessions
        ]
        pending = asyncio.ensure_future(asyncio.gather(*tasks))

        while True:
            if pending.done() and queue.empty():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield _sse(event)

        yield _sse({"event": "run_done", "wall_clock_ms": round((time.monotonic() - started) * 1000, 1)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
