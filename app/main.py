"""FastAPI-стенд: список сценариев, запуск, SSE-поток живой статистики.

Файл заморожен для воркеров дней. Сценарии подключаются автоматически
из day-*/scenario.py — трогать app/ ради нового дня не нужно.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import asdict, dataclass, field
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


def _scenario_public(scenario: Scenario, day: str = "", day_title: str = "") -> dict:
    return {
        "id": scenario.id,
        "day": day,
        "day_title": day_title,
        "title": scenario.title,
        "description": scenario.description,
        "watch_for": scenario.watch_for,
        "layout": scenario.layout,
        "sessions": [asdict(s) for s in scenario.sessions],
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
) -> dict:
    try:
        models = await catalog.fetch_models()
    except Exception as exc:  # каталог недоступен — UI не должен падать
        raise HTTPException(status_code=502, detail=f"каталог моделей недоступен: {exc}") from exc
    needed = tuple(p.strip() for p in requires.split(",") if p.strip())
    filtered = catalog.filter_models(
        models,
        requires=needed,
        exclude_free=exclude_free,
        exclude_temperature_capped=exclude_temperature_capped,
    )
    return {"total": len(models), "count": len(filtered), "models": filtered}


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


# --- разбор пользовательского ввода: и тело /api/chat, и overrides у /api/run ---

_ROLES = ("system", "user", "assistant")

# Что клиент вправе переопределить у колонки сценария. Всё остальное —
# messages, label, depends_on, extra_body — принадлежит автору дня: подмена
# label ломает сопоставление колонок в UI, подмена messages — сам сценарий.
_OVERRIDABLE = ("model", "temperature", "max_tokens")


def _optional_field(payload: dict, name: str, types: tuple, hint: str, where: str = ""):
    """Необязательное поле: либо null, либо нужного типа. Иначе 400 с текстом.

    bool отбрасывается отдельно: в Python True — это int, и «temperature: true»
    иначе доехало бы до провайдера.
    """
    value = payload.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, types):
        raise HTTPException(status_code=400, detail=f"{where}{name}: {hint}")
    return value


def _model_field(payload: dict, where: str = "") -> str:
    """id модели: непустая строка. Пусто — 400, а не падение внутри вызова."""
    value = payload.get("model")
    if isinstance(value, str) and value.strip():
        return value.strip()
    detail = (
        f"{where}model: id модели OpenRouter непустой строкой"
        if where
        else "model обязателен: id модели OpenRouter строкой"
    )
    raise HTTPException(status_code=400, detail=detail)


def _sampling_fields(payload: dict, where: str = "") -> dict:
    """temperature и max_tokens — общие для тела чата и для overrides."""
    temperature = _optional_field(payload, "temperature", (int, float), "число или null", where)
    max_tokens = _optional_field(payload, "max_tokens", (int,), "целое число или null", where)
    if max_tokens is not None and max_tokens <= 0:
        raise HTTPException(
            status_code=400, detail=f"{where}max_tokens: целое число больше нуля или null"
        )
    return {
        "temperature": float(temperature) if temperature is not None else None,
        "max_tokens": max_tokens,
    }


def _parse_overrides(raw: str, sessions: list[Session]) -> dict[str, dict]:
    """Разбирает query-параметр overrides у /api/run.

    Проверяет overrides тот же код, что и тело /api/chat, и проверок ровно
    столько же: кривой ввод обязан получить 400 с текстом, а не 500. До этой
    проверки список вместо объекта ронял AttributeError, лишний ключ —
    TypeError в Session(**fields), а «label» в патче молча переименовывал
    колонку.
    """
    if not raw:
        return {}
    try:
        patch = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"overrides не JSON: {exc}") from exc
    if not isinstance(patch, dict):
        raise HTTPException(
            status_code=400,
            detail="overrides: объект вида {«колонка»: {model, temperature, max_tokens}}",
        )

    labels = {session.label for session in sessions}
    clean: dict[str, dict] = {}
    for label, fields in patch.items():
        where = f"overrides[«{label}»]."
        if label not in labels:
            raise HTTPException(
                status_code=400, detail=f"overrides: колонки «{label}» нет в сценарии"
            )
        if not isinstance(fields, dict):
            raise HTTPException(
                status_code=400,
                detail=f"{where[:-1]}: объект с полями {', '.join(_OVERRIDABLE)}",
            )
        unknown = [key for key in fields if key not in _OVERRIDABLE]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{where[:-1]}: менять можно только {', '.join(_OVERRIDABLE)}, "
                    f"а не {', '.join(sorted(unknown))}"
                ),
            )

        sampling = _sampling_fields(fields, where)
        patched: dict = {}
        # Берём только те поля, что клиент прислал: явный null снимает значение
        # сценария, а пропущенный ключ его не трогает.
        if "model" in fields:
            patched["model"] = _model_field(fields, where)
        for name in ("temperature", "max_tokens"):
            if name in fields:
                patched[name] = sampling[name]
        clean[label] = patched
    return clean


def _chat_session(payload: dict) -> Session:
    """Проверяет тело /api/chat и собирает из него Session.

    Все ошибки — 400 с текстом, который можно показать пользователю: стенд
    не должен отвечать 500 на кривой ввод.
    """
    model = _model_field(payload)

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

    sampling = _sampling_fields(payload)

    stop = _optional_field(payload, "stop", (list,), "список строк или null")
    if stop is not None and not all(isinstance(x, str) for x in stop):
        raise HTTPException(status_code=400, detail="stop: список строк или null")

    response_format = _optional_field(payload, "response_format", (dict,), "объект или null")
    extra_body = _optional_field(payload, "extra_body", (dict,), "объект или null") or {}

    return Session(
        label=str(payload.get("label") or "chat"),
        model=model,
        messages=messages,
        temperature=sampling["temperature"],
        max_tokens=sampling["max_tokens"],
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


@dataclass
class _Outcome:
    """Чем закончилась колонка. Нужно тем, кто ждёт её вывод по depends_on."""

    text: str = ""
    """Последний успешный ответ: его подставляет depends_on."""

    texts: list[str] = field(default_factory=list)
    """Все успешные ответы серии. При repeats=1 — список из одного элемента."""

    ok: bool = False
    finish_reason: str | None = None
    error: str | None = None
    metrics: dict | None = None
    """Метрики последнего успешного прогона."""


def _donor_problem(donor_label: str, donor: _Outcome | None) -> str | None:
    """Почему зависимую колонку запускать нельзя. None — можно.

    Без этой проверки в модель уходила пустая подстановка: донор упал, а
    зависимая колонка всё равно стартовала и отвечала на промпт, из которого
    вырезали половину. На записи это выглядит как «техника не сработала»,
    хотя не сработал вызов, — и стоит денег.
    """
    if donor is None or not donor.ok:
        detail = f": {donor.error}" if donor is not None and donor.error else ""
        return f"колонка пропущена: донор «{donor_label}» не отдал ответ{detail}"
    if not donor.text.strip():
        return f"колонка пропущена: донор «{donor_label}» вернул пустой ответ"
    return None


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
    results: dict[str, _Outcome],
    ready: dict[str, asyncio.Event],
) -> None:
    label = session.label
    outcome = _Outcome()
    try:
        if session.depends_on:
            waiter = ready.get(session.depends_on)
            if waiter is None:
                outcome.error = f"сессии «{session.depends_on}» нет в сценарии"
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

        donor: _Outcome | None = None
        messages = session.messages
        if session.depends_on:
            donor = results.get(session.depends_on)
            problem = _donor_problem(session.depends_on, donor)
            if problem is not None:
                # В модель не идём вовсе: подставлять нечего.
                outcome.error = problem
                await queue.put(
                    {
                        "event": "session_error",
                        "session": label,
                        "message": problem,
                        "reason": "depends_on_failed",
                        "on": session.depends_on,
                    }
                )
                return
            messages = _substitute(messages, donor.text)

        start_event = {
            "event": "session_start",
            "session": label,
            # По resolved_messages клиент перерисовывает ленту чата: для
            # колонки с depends_on это единственный момент, когда виден
            # итоговый промпт после подстановки вывода соседней колонки.
            "resolved_messages": [
                {"role": m.get("role", "?"), "content": m.get("content", "")} for m in messages
            ],
        }
        if session.repeats > 1:
            # Клиенту нужна длина серии заранее: он подписывает блоки
            # «прогон N из M» с первого же прогона.
            start_event["repeats"] = session.repeats
        if donor is not None:
            # Обрыв донора по max_tokens: подставили урезанный промпт — UI
            # помечает это в подписи под лентой, чтобы не гадать на записи.
            start_event["donor"] = {
                "label": session.depends_on,
                "finish_reason": donor.finish_reason,
                "truncated": donor.finish_reason == "length",
            }
        await queue.put(start_event)

        total = max(1, session.repeats)
        # При repeats=1 поток остаётся ровно таким, каким был до появления
        # повторов: ни repeat-событий, ни поля repeat. Сценарии, которые
        # повторов не просили, не должны ничего заметить.
        multi = total > 1

        texts: list[str] = []
        last_metrics: dict | None = None
        failure: str | None = None

        for index in range(total):
            if multi:
                await queue.put(
                    {"event": "repeat_start", "session": label, "repeat": index, "repeats": total}
                )

            text = ""
            final_metrics: dict | None = None
            broken = False
            async for chunk in stream_completion(
                session,
                prompt_override=messages,
                context_length=context_lengths.get(session.model),
            ):
                kind = chunk["type"]
                if kind == "delta":
                    text += chunk["text"]
                    event = {
                        "event": "delta",
                        "session": label,
                        "text": chunk["text"],
                        "metrics": chunk["metrics"],
                    }
                    if multi:
                        event["repeat"] = index
                    await queue.put(event)
                elif kind == "metrics":
                    event = {"event": "metrics", "session": label, "metrics": chunk["metrics"]}
                    if multi:
                        event["repeat"] = index
                    await queue.put(event)
                elif kind == "error":
                    broken = True
                    failure = chunk["message"]
                    # Упавший прогон не отменяет остальные: серия идёт дальше,
                    # а колонка остаётся живой, если хоть один прогон удался.
                    event = {
                        "event": "repeat_error" if multi else "session_error",
                        "session": label,
                        "message": chunk["message"],
                        "metrics": chunk["metrics"],
                    }
                    if multi:
                        event["repeat"] = index
                    await queue.put(event)
                elif kind == "done":
                    text = chunk["text"]
                    final_metrics = chunk["metrics"]

            if not broken:
                texts.append(text)
                last_metrics = final_metrics or last_metrics
                if multi:
                    await queue.put(
                        {
                            "event": "repeat_done",
                            "session": label,
                            "repeat": index,
                            "text": text,
                            "metrics": final_metrics,
                        }
                    )

        outcome = _Outcome(
            text=texts[-1] if texts else "",
            texts=list(texts),
            ok=bool(texts),
            finish_reason=(last_metrics or {}).get("finish_reason"),
            error=failure,
            metrics=last_metrics,
        )
        # Финальные текст и метрики приезжают этим же событием. Для серии
        # это последний удавшийся прогон, а полный список ответов и доля
        # уникальных едут рядом — сумму по серии клиент считает по repeat_done.
        done_event = {
            "event": "session_done",
            "session": label,
            "text": outcome.text,
            "metrics": last_metrics,
        }
        if multi:
            done_event["repeats"] = total
            done_event["texts"] = list(texts)
            done_event["unique"] = len({t.strip() for t in texts})
        await queue.put(done_event)
    except MissingKeyError as exc:
        outcome = _Outcome(error=str(exc))
        await queue.put({"event": "session_error", "session": label, "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 — колонка падает одна, прогон продолжается
        outcome = _Outcome(error=f"{type(exc).__name__}: {exc}")
        await queue.put(
            {"event": "session_error", "session": label, "message": f"{type(exc).__name__}: {exc}"}
        )
    finally:
        # Исход пишем всегда: зависимая колонка должна узнать и об успехе,
        # и о падении, а не гадать, почему записи нет.
        results[label] = outcome
        event = ready.get(label)
        if event is not None:
            event.set()


# --- модель-судья: один вызов после всех колонок, ответ на вопросы задания дня ---

# Дефолтная модель судьи. Заметно крупнее подопытных (в колонках дней стоят
# mini / lite / small / 8b), поддерживает temperature — без этого вызов с
# provider.require_parameters=true просто не пройдёт, — не :free и не :batch.
# Один вызов на прогон, поэтому цена флагмана здесь не проблема.
JUDGE_MODEL = "openai/gpt-4o"
JUDGE_TEMPERATURE = 0.2
"""Судье нужна повторяемость вердикта, а не творчество."""

JUDGE_MAX_TOKENS = 1000
"""Потолок на всякий случай: вердикт должен помещаться в кадр рядом со сводкой."""

_JUDGE_SYSTEM = (
    "Ты — независимый судья на стенде сравнения языковых моделей. "
    "Тебе дают вопросы задания, описание демонстрации и ответы нескольких колонок — "
    "каждая со своими параметрами и метриками. "
    "Ответь по каждому вопросу коротко и по существу, опираясь только на приведённые "
    "ответы и метрики, и закончи одним абзацем — вердиктом. "
    "Пиши по-русски, без вступлений, без пересказа задания и без выдумывания того, "
    "чего в данных нет. Колонку, помеченную как не отработавшая, не оценивай: "
    "скажи, что данных по ней нет."
)


def _judge_column_params(session: Session) -> str:
    """Только то, чем колонка отличается от соседних, — судье это и сравнивать."""
    parts = [f"модель={session.model}"]
    if session.temperature is not None:
        parts.append(f"temperature={session.temperature}")
    if session.max_tokens is not None:
        parts.append(f"max_tokens={session.max_tokens}")
    if session.stop:
        parts.append(f"stop={session.stop}")
    if session.response_format is not None:
        parts.append(f"response_format={session.response_format}")
    return ", ".join(parts)


def _judge_column_answers(outcome: _Outcome) -> str:
    """Судье уходят все ответы серии: без них не ответить про разнообразие."""
    texts = [t.strip() for t in outcome.texts if t.strip()]
    if len(texts) <= 1:
        return "Ответ:\n" + (texts[0] if texts else "")
    unique = len(set(texts))
    head = (
        f"Прогонов: {len(texts)}, уникальных ответов: {unique} из {len(texts)} "
        "(совпадение считается по точному тексту).\n"
        "Ответы по прогонам:"
    )
    body = "\n".join(f"--- прогон {i} ---\n{t}" for i, t in enumerate(texts, 1))
    return f"{head}\n{body}"


def _judge_column_metrics(outcome: _Outcome) -> str:
    metrics = outcome.metrics or {}
    elapsed = metrics.get("elapsed_ms")
    tokens = metrics.get("completion_tokens") or metrics.get("tokens_out")
    cost = metrics.get("cost_usd")
    parts = []
    if elapsed is not None:
        parts.append(f"время {elapsed / 1000:.2f} с")
    if tokens is not None:
        parts.append(f"токенов в ответе {tokens}")
    if cost is not None:
        parts.append(f"стоимость ${cost:.6f}")
    if outcome.finish_reason:
        parts.append(f"finish_reason={outcome.finish_reason}")
    line = ", ".join(parts) or "метрик нет"
    return f"{line} (последний прогон серии)" if len(outcome.texts) > 1 else line


def _judge_messages(
    scenario: Scenario, sessions: list[Session], results: dict[str, _Outcome]
) -> list[dict]:
    questions = "\n".join(f"{i}. {q}" for i, q in enumerate(scenario.judge_questions, 1))
    # watch_for судье не уходит намеренно: это режиссёрская подсказка ведущему,
    # в ней по замыслу написано, какая колонка что покажет и где ошибётся.
    # Отдавать её судье — значит показывать ему ответ до того, как он посмотрит
    # на данные, и заодно заставлять авторов дней портить поле, написанное
    # для человека, ради чистоты вердикта.
    blocks = [
        f"Сценарий: {scenario.title}",
        f"Что демонстрируем: {scenario.description}",
        "",
        "Вопросы задания:",
        questions,
        "",
    ]
    for session in sessions:
        outcome = results.get(session.label) or _Outcome()
        head = f"### Колонка «{session.label}»"
        if not outcome.ok or not outcome.text.strip():
            reason = outcome.error or "ответ пустой"
            blocks.append(f"{head}\nНЕ ОТРАБОТАЛА: {reason}. Вердикт по ней не выноси.\n")
            continue
        blocks.append(
            f"{head}\n"
            + (f"Чем отличается: {session.note}\n" if session.note else "")
            + f"Параметры: {_judge_column_params(session)}\n"
            + f"Метрики: {_judge_column_metrics(outcome)}\n"
            + _judge_column_answers(outcome)
            + "\n"
        )
    return [
        {"role": "system", "content": _JUDGE_SYSTEM},
        {"role": "user", "content": "\n".join(blocks).strip()},
    ]


async def _run_judge(
    scenario: Scenario,
    sessions: list[Session],
    results: dict[str, _Outcome],
    context_lengths: dict[str, int],
):
    """Один вызов к модели-судье. Ошибка судьи прогон не ломает."""
    answered = [
        s for s in sessions
        if (results.get(s.label) or _Outcome()).ok and (results.get(s.label) or _Outcome()).text.strip()
    ]
    if not answered:
        yield {
            "event": "judge_skipped",
            "message": "ни одна колонка не отдала ответ — судить нечего, вызова не было",
        }
        return

    model = scenario.judge_model or JUDGE_MODEL
    # Смысл механизма — что судит другая модель. Совпадение не запрещаем,
    # но показываем: зритель должен видеть, что судья судит сам себя.
    conflicts = sorted({s.label for s in sessions if s.model == model})
    yield {
        "event": "judge_start",
        "model": model,
        "questions": list(scenario.judge_questions),
        "conflicts": conflicts,
    }

    judge = Session(
        label="Судья",
        model=model,
        messages=_judge_messages(scenario, sessions, results),
        temperature=JUDGE_TEMPERATURE,
        max_tokens=JUDGE_MAX_TOKENS,
    )
    try:
        async for chunk in stream_completion(judge, context_length=context_lengths.get(model)):
            kind = chunk["type"]
            if kind == "delta":
                yield {"event": "judge_delta", "text": chunk["text"], "metrics": chunk["metrics"]}
            elif kind == "metrics":
                yield {"event": "judge_metrics", "metrics": chunk["metrics"]}
            elif kind == "error":
                yield {"event": "judge_error", "message": chunk["message"], "metrics": chunk["metrics"]}
            elif kind == "done":
                yield {"event": "judge_done", "text": chunk["text"], "metrics": chunk["metrics"]}
    except MissingKeyError as exc:
        yield {"event": "judge_error", "message": str(exc), "metrics": None}
    except Exception as exc:  # noqa: BLE001 — падает только вердикт, прогон уже состоялся
        yield {"event": "judge_error", "message": f"{type(exc).__name__}: {exc}", "metrics": None}


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

    # overrides: {"<label колонки>": {"model": "...", "temperature": 0.7}}
    patch = _parse_overrides(overrides, scenario.sessions)

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
        results: dict[str, _Outcome] = {}
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
                    "sessions": [asdict(s) for s in sessions],
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

            # wall-clock снимается до судьи: сводка меряет прогон колонок,
            # а не время, которое сверху потратил вердикт.
            wall_clock_ms = round((time.monotonic() - started) * 1000, 1)

            # Судья — один вызов после всех колонок. На прерванном прогоне сюда
            # не доходим: цикл выше выходит по is_disconnected(), и деньги
            # на вердикт по неполным данным не тратятся.
            if scenario.judge_questions:
                async for event in _run_judge(scenario, sessions, results, context_lengths):
                    yield _sse(event)

            yield _sse({"event": "run_done", "wall_clock_ms": wall_clock_ms})
        finally:
            await _cancel_sessions(tasks)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
