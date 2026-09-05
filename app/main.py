"""FastAPI-стенд: список сценариев, запуск, SSE-поток живой статистики.

Файл заморожен для воркеров дней. Сценарии подключаются автоматически
из day-*/scenario.py — трогать app/ ради нового дня не нужно.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import asdict
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query, Request
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


def _scenario_public(scenario: Scenario, day: str = "", day_title: str = "") -> dict:
    return {
        "id": scenario.id,
        "day": day,
        "day_title": day_title,
        "title": scenario.title,
        "description": scenario.description,
        "watch_for": scenario.watch_for,
        "layout": scenario.layout,
        "sessions": [_session_public(s) for s in scenario.sessions],
    }


@app.get("/api/scenarios")
async def list_scenarios() -> dict:
    """Плоский список в порядке дней плюс группировка: сайдбар рисует дни заголовками."""
    days = registry.discover_days()
    scenarios = [
        _scenario_public(scenario, day.id, day.title)
        for day in days
        for scenario in day.scenarios
    ]
    return {
        "has_key": has_key(),
        "scenarios": scenarios,
        "days": [
            {
                "id": day.id,
                "title": day.title,
                "scenario_ids": [s.id for s in day.scenarios],
            }
            for day in days
        ],
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


# --- ручной чат: та же модель, те же метрики, свободный ввод пользователя ---

_ROLES = ("system", "user", "assistant")


def _chat_session(payload: dict) -> Session:
    """Проверяет тело /api/chat и собирает из него Session.

    Все ошибки — 400 с текстом, который можно показать пользователю: стенд
    не должен отвечать 500 на кривой ввод.
    """
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise HTTPException(status_code=400, detail="model обязателен: id модели OpenRouter строкой")

    raw = payload.get("messages")
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=400, detail="messages обязательны: непустой список сообщений")

    messages: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=400, detail=f"messages[{index}] должен быть объектом {{role, content}}"
            )
        role = item.get("role")
        content = item.get("content")
        if role not in _ROLES:
            raise HTTPException(
                status_code=400,
                detail=f"messages[{index}].role должен быть один из {', '.join(_ROLES)}",
            )
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(
                status_code=400, detail=f"messages[{index}].content должен быть непустой строкой"
            )
        messages.append({"role": role, "content": content})

    def optional(name: str, types: tuple, hint: str):
        value = payload.get(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, types):
            raise HTTPException(status_code=400, detail=f"{name}: {hint}")
        return value

    temperature = optional("temperature", (int, float), "число или null")
    max_tokens = optional("max_tokens", (int,), "целое число или null")
    if max_tokens is not None and max_tokens <= 0:
        raise HTTPException(status_code=400, detail="max_tokens: целое число больше нуля или null")

    stop = optional("stop", (list,), "список строк или null")
    if stop is not None and not all(isinstance(x, str) for x in stop):
        raise HTTPException(status_code=400, detail="stop: список строк или null")

    response_format = optional("response_format", (dict,), "объект или null")
    extra_body = optional("extra_body", (dict,), "объект или null") or {}

    return Session(
        label=str(payload.get("label") or "chat"),
        model=model.strip(),
        messages=messages,
        temperature=float(temperature) if temperature is not None else None,
        max_tokens=max_tokens,
        stop=stop or None,
        response_format=response_format,
        extra_body=extra_body,
    )


@app.post("/api/chat")
async def chat(payload: dict = Body(...)) -> StreamingResponse:
    """Свободный запрос к модели: тот же SSE-поток, что и прогон сценария.

    Клиент шлёт всю накопленную ленту колонки, поэтому диалог продолжается,
    а не начинается заново на каждом сообщении.
    """
    if not has_key():
        raise HTTPException(
            status_code=503,
            detail="OPENROUTER_API_KEY не найден: скопируйте .env.example в .env и впишите ключ",
        )

    session = _chat_session(payload)

    try:
        models = await catalog.fetch_models()
        context_length = {m["id"]: m["context_length"] for m in models}.get(session.model)
    except Exception:  # каталог недоступен — заполнение контекста просто не покажем
        context_length = None

    async def event_stream():
        try:
            async for chunk in stream_completion(session, context_length=context_length):
                event = {key: value for key, value in chunk.items() if key != "type"}
                event["event"] = chunk["type"]
                yield _sse(event)
        except MissingKeyError as exc:
            yield _sse({"event": "error", "message": str(exc), "metrics": None})
        except Exception as exc:  # noqa: BLE001 — колонка показывает ошибку, стенд живёт
            yield _sse(
                {"event": "error", "message": f"{type(exc).__name__}: {exc}", "metrics": None}
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
                # Лента чата перерисовывается по resolved_messages: для колонки
                # с depends_on это единственный момент, когда виден итоговый
                # промпт после подстановки вывода соседней колонки.
                "resolved_messages": [
                    {"role": m.get("role", "?"), "content": m.get("content", "")} for m in messages
                ],
                "resolved_prompt": "\n\n".join(
                    f"[{m.get('role', '?')}] {m.get('content', '')}" for m in messages
                ),
            }
        )

        text = ""
        final_metrics = None
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
                        "text": chunk["text"],
                        "metrics": chunk["metrics"],
                    }
                )
            elif kind == "metrics":
                await queue.put(
                    {"event": "metrics", "session": label, "metrics": chunk["metrics"]}
                )
            elif kind == "error":
                await queue.put(
                    {
                        "event": "session_error",
                        "session": label,
                        "message": chunk["message"],
                        "metrics": chunk["metrics"],
                    }
                )
            elif kind == "done":
                text = chunk["text"]
                final_metrics = chunk["metrics"]

        results[label] = text
        # Финальные текст и метрики приезжают этим же событием: колонка
        # заканчивается ровно одним вызовом.
        await queue.put(
            {"event": "session_done", "session": label, "text": text, "metrics": final_metrics}
        )
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


async def _cancel_sessions(tasks: list[asyncio.Task]) -> None:
    """Гасит незавершённые сессии прогона.

    Вызывается, когда SSE-поток закрылся: пользователь закрыл вкладку,
    перезагрузил страницу или перевыбрал сценарий. Без этого задачи продолжают
    качать ответ из OpenRouter до конца — стенд платит за токены, которых никто
    не увидит, а на днях с provider.allow_fallbacks=false брошенный вызов ещё и
    занимает провайдера, к которому пойдёт следующий дубль записи.
    """
    unfinished = [task for task in tasks if not task.done()]
    if not unfinished:
        return
    for task in unfinished:
        task.cancel()
    # cancel() уже разослан: вызовы закроются, даже если нас самих отменяют
    # и дождаться завершения не дадут.
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.gather(*unfinished, return_exceptions=True)


@app.get("/api/run/{scenario_id}")
async def run_scenario(
    request: Request, scenario_id: str, overrides: str = ""
) -> StreamingResponse:
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

        tasks = [
            asyncio.create_task(_run_session(s, context_lengths, queue, results, ready))
            for s in sessions
        ]
        # Всё, что ниже, — под finally: закрытие потока обязано погасить вызовы.
        try:
            yield _sse(
                {
                    "event": "run_start",
                    "scenario": scenario.id,
                    "title": scenario.title,
                    "layout": scenario.layout,
                    "sessions": [_session_public(s) for s in sessions],
                }
            )

            while True:
                # Клиент ушёл со страницы — дальше генерировать некому и незачем.
                if await request.is_disconnected():
                    return
                if queue.empty() and all(task.done() for task in tasks):
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _sse(event)

            yield _sse(
                {"event": "run_done", "wall_clock_ms": round((time.monotonic() - started) * 1000, 1)}
            )
        finally:
            await _cancel_sessions(tasks)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
