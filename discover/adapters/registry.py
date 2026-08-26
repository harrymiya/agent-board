"""Built-in and optional external adapter loading."""

import importlib
import os

from .codex import CodexAdapter
from .generic import ClaudeAdapter, DshAdapter
from .hermes import HermesAdapter
from .opencode import OpenCodeAdapter
from .pi import PiAdapter
from .prime import PrimeAdapter


BUILTIN_ADAPTERS = (
    HermesAdapter,
    PrimeAdapter,
    CodexAdapter,
    OpenCodeAdapter,
    PiAdapter,
    ClaudeAdapter,
    DshAdapter,
)


def _external_adapters():
    """Load dotted modules from AGENTBOARD_ADAPTERS.

    A module may expose ``adapter`` or ``get_adapter()``. A factory can return
    one adapter or an iterable of adapters. Import failures are isolated so a
    broken optional integration cannot take down the built-in dashboard.
    """
    for module_name in filter(None, (os.environ.get("AGENTBOARD_ADAPTERS", "").split(","))):
        try:
            module = importlib.import_module(module_name.strip())
            value = getattr(module, "get_adapter", None)
            value = value() if callable(value) else getattr(module, "adapter", None)
            if value is None:
                continue
            if isinstance(value, (list, tuple, set)):
                yield from value
            else:
                yield value
        except (ImportError, AttributeError, TypeError) as exc:
            print(f"agentboard: skip adapter {module_name!r}: {exc}")


def load_adapters():
    adapters = [factory() for factory in BUILTIN_ADAPTERS]
    adapters.extend(_external_adapters())
    return adapters

