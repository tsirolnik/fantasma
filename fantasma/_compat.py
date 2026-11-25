"""Resolve dotted module paths written before the code moved under ``fantasma``."""
import importlib
from types import ModuleType

LEGACY_TOP_LEVEL = frozenset({
    "agents", "commands", "cost", "fuzzing", "llm", "memory", "models",
    "platforms", "profiles", "scanners", "utils", "web", "config", "main",
})


def import_module_path(module_path: str) -> ModuleType:
    """Import ``module_path``; map pre-0.3 paths like ``memory.mechanisms`` to ``fantasma.memory.mechanisms``."""
    try:
        return importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        top = module_path.split(".", 1)[0]
        if top not in LEGACY_TOP_LEVEL or exc.name not in (top, module_path):
            raise
        return importlib.import_module(f"fantasma.{module_path}")
