"""Shared helpers for the training-mode connection/agent tools
(list_connections / get_connection / create_agent).

Permission tier: a connection is "buildable" for the caller when they hold the
create-agents tier on it — per-connection `create_data_sources` (implied by a
connection `manage_data_sources` grant, org `manage_connections`, or full
admin). Creating an agent additionally requires the org-level
`create_data_source` permission — the same pair the HTTP route
(POST /data_sources) enforces. Checks live here (service/tool layer) because
route decorators do not protect the AI tool path.
"""

import fnmatch
import json
import re
from typing import Iterable, List, Optional, Tuple

_GLOB_META = re.compile(r"[\*\?\[\]]")

# Connection types whose catalog rows are per-file entries and whose scope is
# defined by config (root/globs) or the signed-in account. Mirrors
# schema_context_builder._FILE_SOURCE_TYPES.
FILE_SOURCE_TYPES = {
    "network_dir", "s3", "sharepoint", "onedrive", "google_drive",
    "outlook_mail", "gmail_mail",
}
TOKEN_SCOPED_TYPES = {"onedrive", "google_drive", "outlook_mail", "gmail_mail"}


def conn_data_shape(conn_type: Optional[str]) -> str:
    """The registry data_shape for a connection type ('tables' when unknown)."""
    try:
        from app.schemas.data_source_registry import get_entry
        return get_entry(conn_type).data_shape
    except Exception:
        return "tables"


def compile_patterns(patterns: Optional[Iterable[str]]) -> List[Tuple[str, re.Pattern]]:
    """Compile user filters into case-insensitive regexes.

    A pattern containing glob metacharacters (* ? [ ]) is matched as a glob
    against the WHOLE name; a plain string matches as a substring.
    """
    compiled: List[Tuple[str, re.Pattern]] = []
    for p in patterns or []:
        if not isinstance(p, str) or not p.strip():
            continue
        p = p.strip()
        if _GLOB_META.search(p):
            rx = re.compile(fnmatch.translate(p), re.IGNORECASE)
        else:
            rx = re.compile(".*" + re.escape(p) + ".*", re.IGNORECASE)
        compiled.append((p, rx))
    return compiled


def match_patterns(candidates: Iterable[str], compiled: List[Tuple[str, re.Pattern]]) -> List[str]:
    """Return the original pattern strings that match ANY of the candidate names."""
    hits = []
    for original, rx in compiled:
        if any(c and rx.match(c) for c in candidates):
            hits.append(original)
    return hits


def parse_conn_config(conn) -> dict:
    cfg = getattr(conn, "config", None)
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg or "{}")
        except Exception:
            cfg = {}
    return cfg or {}


def file_scope_of(conn) -> dict:
    """Compact file-scope descriptor for a file-shaped connection."""
    cfg = parse_conn_config(conn)
    globs = [g.strip() for g in re.split(r"[,\n]", str(cfg.get("include_globs") or "")) if g.strip()]
    index_mode = cfg.get("index_mode") or ("content" if cfg.get("index_content", True) else "metadata")
    base = (
        f"s3://{cfg.get('bucket')}/{cfg.get('prefix') or ''}"
        if cfg.get("bucket")
        else cfg.get("root_path")
    )
    return {
        "base": base,
        "include_globs": globs,
        "index_mode": index_mode,
        "token_scoped": getattr(conn, "type", None) in TOKEN_SCOPED_TYPES,
    }


def table_schema_of(row) -> Optional[str]:
    """The database schema recorded on a catalog/table row's metadata."""
    mj = getattr(row, "metadata_json", None)
    if isinstance(mj, str):
        try:
            mj = json.loads(mj or "{}")
        except Exception:
            mj = {}
    if isinstance(mj, dict):
        s = mj.get("schema")
        return str(s) if s else None
    return None


def _prefix_of(name: str) -> str:
    """Grouping prefix for a schemaless catalog name: the schema part when the
    name is dot-qualified, else the token before the first underscore."""
    if "." in name:
        return name.split(".", 1)[0]
    if "_" in name:
        return name.split("_", 1)[0]
    return name


async def table_selection_groups(db, connection_ids: List[str], limit: int = 10) -> List[dict]:
    """Coarse selection menu for the tables catalog of the given connections:
    schema names with counts when schemas exist, else name-prefix clusters.
    Used to turn an unselected create_agent into a clickable clarify question."""
    from sqlalchemy import func, select
    from app.models.connection_table import ConnectionTable

    bind = db.get_bind()
    dialect_name = bind.dialect.name if bind else "sqlite"
    if dialect_name == "postgresql":
        schema_expr = ConnectionTable.metadata_json.op("->>")("schema")
    else:
        schema_expr = func.json_extract(ConnectionTable.metadata_json, "$.schema")

    rows = await db.execute(
        select(schema_expr, func.count(ConnectionTable.id))
        .where(ConnectionTable.connection_id.in_(connection_ids))
        .group_by(schema_expr)
    )
    by_schema = {(str(s) if s else None): int(n or 0) for s, n in rows.all()}

    named = {s: n for s, n in by_schema.items() if s}
    if named:
        groups = [{"label": s, "count": n, "kind": "schema"} for s, n in named.items()]
    else:
        # Schemaless (SQLite/DuckDB): cluster by name prefix.
        name_rows = await db.execute(
            select(ConnectionTable.name).where(ConnectionTable.connection_id.in_(connection_ids))
        )
        counts: dict = {}
        for (name,) in name_rows.all():
            if name:
                counts[_prefix_of(name)] = counts.get(_prefix_of(name), 0) + 1
        groups = [{"label": f"{p}_*" if "." not in p else p, "count": n, "kind": "prefix"}
                  for p, n in counts.items()]
    groups.sort(key=lambda g: -g["count"])
    if len(groups) > limit:
        rest = sum(g["count"] for g in groups[limit:])
        groups = groups[:limit] + [{"label": "other", "count": rest, "kind": "other"}]
    return groups


async def tool_selection_groups(db, connection_ids: List[str], limit: int = 10) -> List[dict]:
    """Coarse selection menu for tool providers: tools clustered by verb prefix
    (get/list/search/create/…), so 'read-only' style choices are one click."""
    from sqlalchemy import select
    from app.models.connection_tool import ConnectionTool

    rows = await db.execute(
        select(ConnectionTool.name).where(ConnectionTool.connection_id.in_(connection_ids))
    )
    counts: dict = {}
    for (name,) in rows.all():
        if name:
            counts[_prefix_of(name)] = counts.get(_prefix_of(name), 0) + 1
    groups = [{"label": f"{p}_*", "count": n, "kind": "tool_prefix"} for p, n in counts.items()]
    groups.sort(key=lambda g: -g["count"])
    return groups[:limit]


def can_create_on_connection(resolved, connection_id: str) -> bool:
    """The per-connection half of the create-agents tier."""
    return resolved.has_resource_permission("connection", str(connection_id), "create_data_sources")


def can_create_agents(resolved) -> bool:
    """The org-level half (full_admin_access passes implicitly)."""
    return resolved.has_org_permission("create_data_source")
