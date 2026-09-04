"""Per-type connection field specs for the admin UI's Add Connection form.

Each data source type has its own config and credential fields — PostgreSQL
takes host/port/database, a QVD source takes file-path globs, BigQuery a
service-account JSON. Those field definitions are the backend's own Pydantic
config classes (`app/schemas/data_sources/configs.py`) rendered as JSON Schema
with `ui:type` hints — the same source the Bow app's ConnectForm renders from.

The registry that maps a type to those classes imports `app.settings`, which
the edge agent does not carry, so the specs are snapshotted to `type_specs.json`
(by tools/agent/generate_edge_type_specs.py, run in the backend venv) and shipped
alongside the agent. This module just loads and serves that bundle.
"""

from __future__ import annotations

import functools
import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_BUNDLE_PATH = Path(__file__).parent / "type_specs.json"


@functools.lru_cache(maxsize=1)
def _bundle() -> dict[str, Any]:
    try:
        return json.loads(_BUNDLE_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:  # pragma: no cover - packaging error
        logger.error("edge_agent.type_specs.load_failed", extra={"error": str(e)})
        return {"version": 0, "types": {}}


def catalog() -> list[dict[str, str]]:
    """Grid metadata for the type picker: one row per type, sorted by category
    then title. Just what the picker needs — the full field spec is fetched per
    type when one is chosen."""
    types = _bundle().get("types", {})
    rows = [
        {"type": t, "title": s.get("title", t), "category": s.get("category", "databases")}
        for t, s in types.items()
    ]
    return sorted(rows, key=lambda r: (r["category"], r["title"].lower()))


def get_spec(type_name: str) -> Optional[dict[str, Any]]:
    """The full field spec for one type: title, category, config JSON-schema,
    auth options, and credentials JSON-schema per auth mode. None if unknown."""
    return _bundle().get("types", {}).get(type_name)


def spec_types() -> list[str]:
    return sorted(_bundle().get("types", {}).keys())
