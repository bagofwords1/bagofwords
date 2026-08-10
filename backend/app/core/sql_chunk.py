"""Bind-parameter-safe chunking for `IN (...)` clauses.

Every driver caps how many bind parameters one statement may carry, and the
ceiling is low enough that catalog-sized lists blow straight through it:

  * PostgreSQL's wire protocol encodes the parameter count as an int16, so a
    statement may carry at most **32767** of them. asyncpg raises
    ``InterfaceError: the number of query arguments cannot exceed 32767``
    before the query is even sent.
  * SQLite's ``SQLITE_MAX_VARIABLE_NUMBER`` defaults to 32766 (999 before 3.32).

A `col.in_(values)` clause spends one parameter per element, so any list whose
length tracks a *catalog* — table names, table ids, per-user overlay rows — is a
latent hard failure that only shows up on the largest customer. It is not a
slowdown: the request 500s and the whole transaction rolls back.

Prefer a subquery when the values come from the database anyway
(``col.in_(select(...))`` costs zero parameters). Reach for `chunked` when the
list is genuinely in Python.

    for chunk in chunked(table_ids):
        rows += (await db.execute(select(T).where(T.id.in_(chunk)))).scalars().all()
"""
from __future__ import annotations

from typing import Iterator, Sequence, TypeVar

T = TypeVar("T")

# Well under every driver's ceiling, and small enough that the per-statement
# planning cost stays flat. Matches the chunk size already used by
# ConnectionService._user_visible_table_names and the overlay column sync.
BIND_CHUNK_SIZE = 500


def chunked(values: Sequence[T], size: int = BIND_CHUNK_SIZE) -> Iterator[Sequence[T]]:
    """Yield `values` in slices of at most `size`, so each resulting IN clause
    stays under the driver's bind-parameter ceiling. Yields nothing for an
    empty input — callers that need an "empty means match nothing" branch must
    handle it before looping."""
    for i in range(0, len(values), size):
        yield values[i:i + size]
