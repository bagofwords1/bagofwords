"""Shared-session hygiene helpers for the agent loop.

The agent runs a whole turn through one long-lived ``AsyncSession``
(``self.db``). When any statement on that session raises, the underlying
Postgres transaction is left in an aborted state and asyncpg rejects every
subsequent command with ``InFailedSQLTransactionError`` until a ROLLBACK is
issued. SQLAlchemy mirrors this: the session goes ``is_active is False`` and
raises ``PendingRollbackError`` on the next flush.

A common failure mode is a broad ``except`` that swallows a failed statement
without rolling back. The next unrelated query then autoflushes any pending
dirty state (e.g. an in-memory ``latest_seq`` bump from
``ProjectManager.next_seq``) and blows up far from the real cause, surfacing
as::

    UPDATE agent_executions SET latest_seq=... WHERE id=...
    -> current transaction is aborted, commands ignored ...

``rollback_if_poisoned`` heals the session at such a boundary so the pending
in-memory changes are discarded cleanly and the following statements run in a
fresh transaction. It only rolls back when the session is actually poisoned:
an unconditional rollback would expire every loaded instance and turn later
plain attribute access into an async lazy-load (``MissingGreenlet``).
"""

from sqlalchemy.ext.asyncio import AsyncSession


async def rollback_if_poisoned(db: AsyncSession) -> bool:
    """Roll back ``db`` iff its transaction is poisoned.

    Returns ``True`` when a rollback was performed, ``False`` when the session
    was already healthy (or the check/rollback itself failed and was ignored).
    Never raises — it is meant to be safe to call from error-recovery paths.
    """
    try:
        if db is None:
            return False
        # ``is_active`` is False once a statement has failed and the session is
        # awaiting a rollback. That is exactly the state that poisons every
        # later query's autoflush.
        if db.is_active:
            return False
        await db.rollback()
        return True
    except Exception:
        # Best-effort: recovery paths must not raise a second exception.
        return False
