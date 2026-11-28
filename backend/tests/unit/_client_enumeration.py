"""Every data-source client class production can construct, for tests that
must account for each one (renderer completeness, guard classification).

`resolve_client_class` is called for every `REGISTRY` key, so entries
without a `client_path` resolve through its naming fallback exactly as in
production. Classes defined in `app.data_sources.fast` that define their own
`execute_query` are added: an activated custom table hands the agent one.
Nothing is caught: a class that cannot be imported cannot be accounted for,
so the import error fails the test.
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil


def enumerated_client_classes() -> list[type]:
    """Distinct classes, in `REGISTRY` key order, then `fast/` module order."""
    from app.schemas.data_source_registry import REGISTRY, resolve_client_class

    classes: list[type] = []
    for key in REGISTRY:
        cls = resolve_client_class(key)
        if cls not in classes:
            classes.append(cls)

    import app.data_sources.fast as fast

    for info in pkgutil.iter_modules(fast.__path__):
        module = importlib.import_module(f"{fast.__name__}.{info.name}")
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ == module.__name__ and "execute_query" in cls.__dict__ and cls not in classes:
                classes.append(cls)
    return classes
