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

    repeats: int = 1
    """Сколько раз прогнать сессию (День 4: 5 сэмплов на температуру).

    Повторы идут последовательно внутри колонки, UI показывает их списком
    и считает долю уникальных ответов.
    """

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
    """Один день челленджа = один сценарий в списке стенда."""

    id: str
    """Совпадает с именем папки: "day-04"."""

    title: str
    description: str
    """Показывается до нажатия «Старт» — что это за демонстрация."""

    watch_for: str
    """На что смотреть на записи экрана. Выводится рядом с описанием."""

    sessions: list[Session]

    layout: str = "split"
    """split | single."""
