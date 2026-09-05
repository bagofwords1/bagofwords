"""Dialect-agnostic foreign-key reflection for SQLAlchemy-backed clients.

Every SQL client here builds the same `{(schema, table): Table}` dict and, until
now, handed it back with `fks` empty. That is not a neutral omission: the schema
context renders FKs as real DDL (`prompt_formatters.TableFormatter`), so a table
with no `fks` reaches the agent as `CREATE TABLE ... ( ... )` with the foreign
key clauses simply missing — which reads as *this table has no relationships*,
not *relationships unknown*. The agent then invents a join or refuses one.

Rather than hand-writing `pg_constraint` / `KEY_COLUMN_USAGE` /
`sys.foreign_key_columns` / `ALL_CONSTRAINTS` per client, this reads them
through SQLAlchemy's own dialects, which already contain exactly those queries.
One implementation covers every client that connects through `engine_pool`.

Two things callers must get right, both enforced by the signature:

* **`name_fn`** composes the target's display name. It has to produce the *same*
  string the client uses for its own `Table.name`, because
  `schema_context_builder` resolves an edge by exact string match against the
  visible table names — a convention mismatch drops the FK silently rather than
  erroring. Postgres and MSSQL both name tables `schema.table`, so both pass
  `lambda s, t: f"{s}.{t}"`.
* **`schemas`** must be the same list the table query was filtered by, or
  reflection walks the whole catalog and pays for tables the caller discarded.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

import sqlalchemy

from app.ai.prompt_formatters import ForeignKey, Table, TableColumn

logger = logging.getLogger(__name__)

TableKey = tuple[str, str]


def _dtype_of(table: Table | None, column_name: str) -> str | None:
    """Best-effort dtype for a column already collected on `table`.

    Reflection returns column *names* only, so types are borrowed from the
    columns the caller already read. A miss is fine — dtype is advisory on an
    FK, and `TableColumn.dtype` is optional.
    """
    if table is None:
        return None
    for col in table.columns or []:
        if col.name == column_name:
            return col.dtype
    return None


def _resolve_target(
    tables: dict,
    key_fn: Callable[[str, str], object],
    candidate_schemas: Sequence[str | None],
    referred_schema: str | None,
    referred_table: str,
    source_schema: str,
) -> TableKey:
    """Decide which collected table an edge actually points at.

    `referred_schema` is not reliably populated. PostgreSQL's dialect reports
    it as None whenever the referenced table sits anywhere on the connection's
    `search_path` — and this client sets `search_path` to every schema the user
    selected, so a genuine cross-schema FK (`sales.orders` -> `ref.customers`)
    comes back schema-less and indistinguishable from a same-schema one.
    Assuming the source's own schema in that case produced `sales.customers`:
    an edge that is well-formed, plausible, and points at nothing.

    So resolve against the tables actually collected rather than trusting the
    dialect: prefer the source's own schema (the common case), then a unique
    match by table name across the selected schemas, and only then fall back.
    The fallback still emits the edge — an unresolvable reference is data worth
    keeping, and callers can report it — it simply won't match a table.
    """
    if referred_schema:
        return (referred_schema, referred_table)

    if key_fn(source_schema, referred_table) in tables:
        return (source_schema, referred_table)

    candidates = [
        sch for sch in candidate_schemas
        if sch is not None and key_fn(sch, referred_table) in tables
    ]
    if len(candidates) == 1:
        return (candidates[0], referred_table)

    return (source_schema, referred_table)


def _reflect_schema(
    inspector: sqlalchemy.engine.Inspector, schema: str | None
) -> dict[TableKey, list[dict]]:
    """One batched reflection call for a schema, or {} if the dialect refuses.

    `get_multi_foreign_keys` is one round trip per schema; the per-table
    `get_foreign_keys` loop it replaces would be one per table, which on a
    catalog of a few thousand tables is the difference between a second and a
    coffee break. Dialects that predate the multi-form or reject it fall back to
    an empty result for that schema rather than failing the whole sync.
    """
    try:
        raw = inspector.get_multi_foreign_keys(schema=schema)
    except Exception:
        logger.debug(
            "FK reflection unavailable for schema %r", schema, exc_info=True
        )
        return {}

    normalized: dict[TableKey, list[dict]] = {}
    for key, fk_list in (raw or {}).items():
        # SQLAlchemy keys these as (schema, table); schema is None for the
        # connection's default schema, which the caller names explicitly.
        key_schema, key_table = key if isinstance(key, tuple) else (schema, key)
        normalized[(key_schema or schema or "", key_table)] = fk_list or []
    return normalized


def attach_foreign_keys(
    connection: sqlalchemy.engine.Connection,
    tables: dict,
    schemas: Sequence[str] | None,
    name_fn: Callable[[str, str], str],
    key_fn: Callable[[str, str], object] | None = None,
) -> int:
    """Populate `Table.fks` in place. Returns the number of edges attached.

    Best-effort by contract: introspection that fails, times out, or hits a
    permission wall must not cost the caller its tables, which are the thing the
    user actually asked for. Every failure path logs and returns what it has.

    Composite constraints are emitted as one `ForeignKey` per column pair —
    `ForeignKey` holds a single column, so a two-column constraint becomes two
    edges. That is lossy (the pair is no longer known to travel together) and is
    the reason a renderer should group edges by target before drawing them.

    `key_fn` maps a (schema, table) pair onto the caller's own dict key, because
    the clients do not agree on one. The schema-qualified ones key by tuple
    (the default); the single-database ones — MySQL, MariaDB — key by bare table
    name, and would silently match nothing against a tuple lookup.
    """
    if not tables:
        return 0

    try:
        inspector = sqlalchemy.inspect(connection)
    except Exception:
        logger.warning("Could not build inspector for FK reflection", exc_info=True)
        return 0

    key_fn = key_fn or (lambda schema, table: (schema, table))

    # Reflect only what the caller kept. Given no explicit schema list, a
    # tuple-keyed caller still names its own schemas, so derive them rather than
    # walking the whole catalog; a bare-keyed caller addresses one database, so
    # the dialect's default schema is the whole of it.
    target_schemas: Sequence[str | None]
    if schemas:
        target_schemas = list(schemas)
    else:
        derived = {k[0] for k in tables if isinstance(k, tuple) and k[0]}
        target_schemas = sorted(derived) if derived else [None]

    attached = 0
    for schema in target_schemas:
        for key, fk_list in _reflect_schema(inspector, schema).items():
            table = tables.get(key_fn(*key))
            if table is None:
                continue
            if table.fks is None:
                table.fks = []

            for fk in fk_list:
                constrained = fk.get("constrained_columns") or []
                referred = fk.get("referred_columns") or []
                referred_table = fk.get("referred_table")
                if not (constrained and referred and referred_table):
                    continue
                target_key = _resolve_target(
                    tables, key_fn, target_schemas,
                    fk.get("referred_schema"), referred_table, key[0],
                )
                target_name = name_fn(*target_key)
                target_table = tables.get(key_fn(*target_key))

                # strict=False on purpose: a dialect returning mismatched
                # column lists is malformed input, not a reason to abort a
                # schema sync — pair what we can and move on.
                for local_col, remote_col in zip(constrained, referred, strict=False):
                    table.fks.append(
                        ForeignKey(
                            column=TableColumn(
                                name=local_col,
                                dtype=_dtype_of(table, local_col),
                            ),
                            references_name=target_name,
                            references_column=TableColumn(
                                name=remote_col,
                                dtype=_dtype_of(target_table, remote_col),
                            ),
                        )
                    )
                    attached += 1

    return attached
