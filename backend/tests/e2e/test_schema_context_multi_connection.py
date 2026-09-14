"""The AGENT's schema context must cover every connection, scoped per user.

Companion to test_multi_connection_tables_selector.py. That one covers what the
tables selector renders; this one covers what the model actually reasons over —
and the two used to disagree, because `SchemaContextBuilder` answered "what may
this user see" independently, from `data_source.connections[0]`:

    conns = list(getattr(ds, 'connections', None) or [])
    conn = conns[0] if conns else None

Same two failures as the selector bug, decided by connection order:

  * delegated connection first  -> the whole data source was served from the
    per-user overlay, which only describes delegated connections, so every
    system_only connection's tables vanished from the prompt. Asked "what are
    your connections?", a three-connection agent answered "1 PowerBI
    connection ... 18 tables total" (reproduced live against Claude Haiku).
  * delegated connection second -> no scoping at all, so one user's prompt
    carried another user's delegated tables.

Both call sites now share DataSourceService.classify_connection_access, and the
builder unions the two sources instead of choosing between them: overlay rows
(which carry per-user column masking) for delegated connections, canonical rows
for open ones.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      python -m pytest tests/e2e/test_schema_context_multi_connection.py -v -s
"""
import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.ai.context.builders.schema_context_builder import SchemaContextBuilder

# Reuse the three-connection fixture and the delegated-client fake.
from tests.e2e.test_multi_connection_tables_selector import (  # noqa: E402
    _seed, _sync, _install_fake_pbi, WAREHOUSE_TABLES, PBI_BY_USER,
)


def _run(coro):
    return asyncio.run(coro)


async def _activate_all(ids):
    """Activate every canonical row, as an agent manager would in the tables
    wizard. A table a delegated user discovered themselves is created INACTIVE
    on purpose (their upstream access must not widen the agent past what its
    manager selected), and the agent context is activation-gated — so without
    this the user-specific models are correctly absent and the per-user
    assertions below would be testing the activation gate, not the scoping."""
    async with async_session_maker() as db:
        rows = (await db.execute(
            select(DataSourceTable).where(DataSourceTable.datasource_id == ids["ds_id"])
        )).scalars().all()
        for r in rows:
            r.is_active = True
            db.add(r)
        await db.commit()
        return len(rows)


async def _context_for(ids, user_id):
    """Exactly what the planner is handed for this user on this agent."""
    async with async_session_maker() as db:
        ds = (await db.execute(
            select(DataSource).options(selectinload(DataSource.connections))
            .where(DataSource.id == ids["ds_id"])
        )).scalar_one()
        org = await db.get(Organization, ids["org_id"])
        user = await db.get(User, user_id)
        ctx = await SchemaContextBuilder(
            db=db, data_sources=[ds], organization=org, report=None, user=user
        ).build(with_stats=False)
        return ctx.render() if ctx else ""


@pytest.mark.e2e
@pytest.mark.parametrize("delegated_first", [True, False],
                         ids=["delegated-first", "delegated-second"])
def test_agent_context_covers_every_connection(monkeypatch, delegated_first):
    ids = _run(_seed(delegated_first))
    _install_fake_pbi(monkeypatch, ids, [])
    u1 = ids["users"]["analyst1"]
    u2 = ids["users"]["analyst2"]
    _run(_sync(ids, u1))
    _run(_sync(ids, u2))
    _run(_activate_all(ids))

    ctx1 = _run(_context_for(ids, u1))
    ctx2 = _run(_context_for(ids, u2))

    # 1. THE BUG: the warehouse tables must be in the prompt whichever
    #    connection sorts first. Without the fix, `delegated-first` served the
    #    overlay alone and none of these appeared.
    for name in WAREHOUSE_TABLES:
        assert name in ctx1, f"{name} missing from analyst1's agent context"
        assert name in ctx2, f"{name} missing from analyst2's agent context"

    # 2. Each analyst's OWN delegated tables are there...
    assert "ModelU1/T9" in ctx1
    assert "ModelU2/T8" in ctx2

    # 3. ...and the other's are not. `delegated-second` used to skip scoping
    #    entirely and put both users' models in both prompts.
    assert "ModelU2/T8" not in ctx1, "analyst1's prompt carries analyst2's model"
    assert "ModelU1/T9" not in ctx2, "analyst2's prompt carries analyst1's model"


@pytest.mark.e2e
def test_agent_context_is_independent_of_connection_order(monkeypatch):
    """The same user on the same agent must get the same context regardless of
    the order the relationship happens to yield connections in — the
    nondeterminism that made this bug intermittent in production."""
    contexts = {}
    for delegated_first in (True, False):
        ids = _run(_seed(delegated_first))
        _install_fake_pbi(monkeypatch, ids, [])
        u1 = ids["users"]["analyst1"]
        # Sync BOTH analysts so analyst2's model genuinely exists in the
        # catalog — otherwise the "analyst1 must not see it" assertion below
        # passes for the wrong reason (there is nothing to see).
        _run(_sync(ids, u1))
        _run(_sync(ids, ids["users"]["analyst2"]))
        _run(_activate_all(ids))
        ctx = _run(_context_for(ids, u1))
        # Table names only: ids and generated names differ between seeds.
        names = sorted({
            n for n in (WAREHOUSE_TABLES + PBI_BY_USER["analyst1"] + ["ModelU2/T8"])
            if n in ctx
        })
        contexts[delegated_first] = names

    assert contexts[True] == contexts[False], (
        f"context depends on connection order:\n"
        f"  delegated-first={contexts[True]}\n  delegated-second={contexts[False]}"
    )
    assert "ModelU2/T8" not in contexts[True]
