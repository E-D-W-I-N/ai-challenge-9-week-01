"""Автоподхват сценариев из day-*/scenario.py.

`day-01` — невалидное имя пакета в Python (дефис), поэтому файлы грузятся
через importlib.util.spec_from_file_location, а не обычным импортом.
"""

from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path

from .config import ROOT
from .schema import Scenario

_ERRORS: dict[str, str] = {}


def _load_file(path: Path) -> Scenario | None:
    module_name = f"_scenario_{path.parent.name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        _ERRORS[path.parent.name] = "не удалось создать spec"
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        _ERRORS[path.parent.name] = traceback.format_exc(limit=3)
        return None

    scenario = getattr(module, "SCENARIO", None)
    if scenario is None:
        factory = getattr(module, "get_scenario", None)
        if callable(factory):
            try:
                scenario = factory()
            except Exception:
                _ERRORS[path.parent.name] = traceback.format_exc(limit=3)
                return None

    if not isinstance(scenario, Scenario):
        _ERRORS[path.parent.name] = (
            "scenario.py должен объявить SCENARIO: Scenario "
            "(или get_scenario() -> Scenario)"
        )
        return None
    return scenario


def discover() -> list[Scenario]:
    """Сканирует day-*/scenario.py при каждом вызове — правки видны без перезапуска."""
    _ERRORS.clear()
    scenarios: list[Scenario] = []
    for folder in sorted(ROOT.glob("day-*")):
        entry = folder / "scenario.py"
        if not entry.is_file():
            continue
        scenario = _load_file(entry)
        if scenario is not None:
            scenarios.append(scenario)
    return scenarios


def errors() -> dict[str, str]:
    """Ошибки загрузки последнего discover() — показываются в UI, а не глотаются."""
    return dict(_ERRORS)


def get(scenario_id: str) -> Scenario | None:
    for scenario in discover():
        if scenario.id == scenario_id:
            return scenario
    return None
