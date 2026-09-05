"""Контракт сценария. Единственный модуль, который импортируют day-NN/scenario.py.

Файл заморожен: воркеры дней его не меняют. Нужна правка — escalation оркестратору.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Session:
    """Одна колонка split-screen — один вызов (или серия вызовов) к OpenRouter."""

    label: str
    """Заголовок колонки в UI."""

    model: str
    """id модели OpenRouter, например "meta-llama/llama-3.1-8b-instruct"."""

    messages: list[dict]
    """Сообщения в формате OpenAI chat: [{"role": "user", "content": "..."}]."""

    temperature: float | None = None
    max_tokens: int | None = None
    stop: list[str] | None = None
    response_format: dict | None = None

    depends_on: str | None = None
    """label другой сессии этого же сценария.

    Сессия стартует только после её завершения, а её текст доступен как
    подстановка {{depends_on}} в любом content этой сессии.
    День 3: модель сначала пишет промпт, потом он применяется.
    """

    note: str = ""
    """Подпись под колонкой: что именно тут показываем."""

    extra_body: dict = field(default_factory=dict)
    """Дополнительные поля запроса к OpenRouter (seed, provider.order и т.п.).

    Мержится поверх тела запроса. app/llm.py уже добавляет
    provider.require_parameters = true — переопределять не нужно,
    но можно дополнить: {"provider": {"allow_fallbacks": False}}.
    """


@dataclass
class Scenario:
    """Один сценарий стенда. День отдаёт список таких сценариев."""

    id: str
    """Уникален глобально и начинается с имени папки: "day-02-format".

    День с одним сценарием может назвать его просто "day-02".
    """

    title: str
    description: str
    """Показывается до нажатия «Старт» — что это за демонстрация."""

    watch_for: str
    """На что смотреть на записи экрана. Выводится рядом с описанием."""

    sessions: list[Session]

    layout: str = "split"
    """split | single."""

    judge_questions: list[str] = field(default_factory=list)
    """Вопросы задания дня, своими словами — на них отвечает модель-судья.

    После того как отработали все колонки, стенд делает один дополнительный
    вызов к отдельной модели и стримит её ответ в блок «Вердикт» под колонками.
    Судье уходят эти вопросы, description и watch_for сценария, а по каждой
    колонке — label, note, отличающие её параметры, метрики и полный ответ.

    Пустой список (по умолчанию) — блока «Вердикт» нет и вызова нет: день,
    который ничего не сравнивает, не должен платить за судью.
    """

    judge_model: str | None = None
    """Модель-судья. None — дефолт стенда (app.main.JUDGE_MODEL).

    Смысл механизма в том, что судит **другая** модель, а не одна из колонок.
    Совпадение с моделью колонки не запрещено, но стенд помечает его в UI.
    """
