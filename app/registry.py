"""Автоподхват сценариев из day-*/scenario.py.

Один день отдаёт один или несколько сценариев: основной контракт —
`SCENARIOS: list[Scenario]`, одиночный `SCENARIO: Scenario` продолжает работать.
Порядок внутри списка значим — он задаёт порядок сценариев дня в UI.

`day-01` — невалидное имя пакета в Python (дефис), поэтому файлы грузятся
через importlib.util.spec_from_file_location, а не обычным импортом.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from .config import ROOT
from .schema import Scenario

_ERRORS: dict[str, str] = {}
_DAY_RE = re.compile(r"^day-(\d+)")


@dataclass
class Day:
    """Сценарии одного дня в порядке из SCENARIOS."""

    id: str
    """Имя папки: "day-02"."""

    title: str
    """Человекочитаемый заголовок для сайдбара: "День 02"."""

    scenarios: list[Scenario] = field(default_factory=list)


def day_title(folder_name: str) -> str:
    """day-02 -> «День 02». Незнакомое имя папки отдаётся как есть."""
    match = _DAY_RE.match(folder_name)
    return f"День {match.group(1)}" if match else folder_name


def _fail(day: str, message: str) -> None:
    """Копит ошибки по дню: одна папка может нарушить контракт не один раз."""
    previous = _ERRORS.get(day)
    _ERRORS[day] = f"{previous}\n{message}" if previous else message


def _day_sort_key(path: Path) -> tuple[int, int, str]:
    match = _DAY_RE.match(path.name)
    if match is None:
        return (1, 0, path.name)
    return (0, int(match.group(1)), path.name)


def _extract(module, day: str) -> list[Scenario] | None:
    """SCENARIOS / get_scenarios() / SCENARIO / get_scenario() — в этом порядке."""
    for attr, many in (("SCENARIOS", True), ("SCENARIO", False)):
        value = getattr(module, attr, None)
        if value is None:
            continue
        return _coerce(value, many, day, attr)

    for attr, many in (("get_scenarios", True), ("get_scenario", False)):
        factory = getattr(module, attr, None)
        if not callable(factory):
            continue
        try:
            value = factory()
        except Exception:
            _fail(day, traceback.format_exc(limit=3))
            return None
        return _coerce(value, many, day, f"{attr}()")

    _fail(
        day,
        "scenario.py должен объявить SCENARIOS: list[Scenario] "
        "(или SCENARIO: Scenario / get_scenarios() / get_scenario())",
    )
    return None


def _coerce(value, many: bool, day: str, source: str) -> list[Scenario] | None:
    if not many:
        if not isinstance(value, Scenario):
            _fail(day, f"{source} должен быть Scenario, а не {type(value).__name__}")
            return None
        return [value]

    if not isinstance(value, (list, tuple)):
        _fail(day, f"{source} должен быть списком Scenario, а не {type(value).__name__}")
        return None
    scenarios = list(value)
    if not scenarios:
        _fail(day, f"{source} пуст — день не отдал ни одного сценария")
        return None
    for index, item in enumerate(scenarios):
        if not isinstance(item, Scenario):
            _fail(day, f"{source}[{index}] должен быть Scenario, а не {type(item).__name__}")
            return None
    return scenarios


def _load_file(path: Path) -> list[Scenario] | None:
    day = path.parent.name
    module_name = f"_scenario_{day.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        _fail(day, "не удалось создать spec")
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        _fail(day, traceback.format_exc(limit=3))
        return None
    return _extract(module, day)


def _validate(day: str, scenarios: list[Scenario], claimed: dict[str, str]) -> bool:
    """id уникален глобально и начинается с имени папки. Ломается только виноватый день."""
    ok = True
    inside: set[str] = set()
    for scenario in scenarios:
        sid = scenario.id
        if not isinstance(sid, str) or not sid.startswith(day):
            _fail(day, f"id «{sid}» должен начинаться с имени папки «{day}» (например «{day}-format»)")
            ok = False
            continue
        if sid in inside:
            _fail(day, f"id «{sid}» повторяется внутри дня — id уникален глобально")
            ok = False
            continue
        owner = claimed.get(sid)
        if owner is not None:
            _fail(day, f"id «{sid}» уже занят папкой «{owner}» — id уникален глобально")
            ok = False
            continue
        inside.add(sid)
    return ok


def discover_days() -> list[Day]:
    """Сканирует day-*/scenario.py при каждом вызове — правки видны без перезапуска.

    Дни идут по возрастанию номера, сценарии внутри дня — в порядке из SCENARIOS.
    День с нарушенным контрактом целиком уходит в errors(), остальные грузятся.
    """
    _ERRORS.clear()
    claimed: dict[str, str] = {}
    days: list[Day] = []
    folders = sorted((p for p in ROOT.glob("day-*") if p.is_dir()), key=_day_sort_key)
    for folder in folders:
        entry = folder / "scenario.py"
        if not entry.is_file():
            continue
        scenarios = _load_file(entry)
        if scenarios is None:
            continue
        if not _validate(folder.name, scenarios, claimed):
            continue
        for scenario in scenarios:
            claimed[scenario.id] = folder.name
        days.append(Day(id=folder.name, title=day_title(folder.name), scenarios=scenarios))
    return days


def discover() -> list[Scenario]:
    """Плоский список всех сценариев в порядке дней и порядке внутри дня."""
    return [scenario for day in discover_days() for scenario in day.scenarios]


def errors() -> dict[str, str]:
    """Ошибки загрузки последнего discover() — показываются в UI, а не глотаются."""
    return dict(_ERRORS)


def get(scenario_id: str) -> Scenario | None:
    for scenario in discover():
        if scenario.id == scenario_id:
            return scenario
    return None
