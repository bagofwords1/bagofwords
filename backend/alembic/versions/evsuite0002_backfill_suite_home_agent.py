"""Backfill a home agent onto suites whose cases all agree on one

Revision ID: evsuite0002
Revises: evsuite0001
Create Date: 2026-08-07

``evsuite0001`` added ``TestSuite.data_source_id`` and left every existing row
NULL — i.e. org-wide. That is the safe default but the wrong answer for the
common case: before suites had a home, an agent's tests were filed into a suite
that was org-wide only because there was no alternative. Left alone, those
suites become org-level to rename or delete, and stop appearing under the agent
whose tests they hold.

A suite is homed only when the evidence is unambiguous: every non-deleted case
in it targets exactly ONE agent, and it is the same agent for all of them.
Anything else stays org-wide —

  - a suite with no cases (nothing to infer from),
  - a suite holding an agent-less case (that case runs against every agent, so
    the suite genuinely spans the org),
  - a suite holding a routing eval targeting two agents,
  - a suite whose cases disagree about which agent they belong to.

The default "Drafts" bucket is skipped by name. It is looked up as
``(organization_id, name='Drafts', data_source_id IS NULL)``; homing it would
leave that lookup creating a second one. Its single-agent cases are not stranded
— the agent tree surfaces them under "Unfiled", where they can be dragged into a
real suite.

Data-only and idempotent: re-running changes nothing, since a homed suite is no
longer NULL. Nothing is deleted, and downgrade clears only what this set.
"""
import json

from alembic import op
import sqlalchemy as sa


revision = 'evsuite0002'
down_revision = 'evsuite0001'
branch_labels = None
depends_on = None

DRAFTS_NAME = "Drafts"


def _agent_ids(raw):
    """Normalize data_source_ids_json across sqlite (TEXT) and postgres (JSON)."""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if x]


def upgrade() -> None:
    bind = op.get_bind()

    suites = bind.execute(sa.text(
        "SELECT id FROM test_suites "
        "WHERE data_source_id IS NULL AND deleted_at IS NULL AND name <> :drafts"
    ), {"drafts": DRAFTS_NAME}).fetchall()

    live_agents = {
        str(r[0]) for r in bind.execute(sa.text("SELECT id FROM data_sources")).fetchall()
    }

    homed = 0
    for (suite_id,) in suites:
        rows = bind.execute(sa.text(
            "SELECT data_source_ids_json FROM test_cases "
            "WHERE suite_id = :sid AND deleted_at IS NULL"
        ), {"sid": suite_id}).fetchall()
        if not rows:
            continue  # empty suite — nothing to infer from

        targets = {tuple(sorted(_agent_ids(r[0]))) for r in rows}
        if len(targets) != 1:
            continue  # cases disagree
        only = next(iter(targets))
        if len(only) != 1:
            continue  # agent-less, or spans several agents
        agent_id = only[0]
        if agent_id not in live_agents:
            continue  # the FK would fail; leave it org-wide

        bind.execute(sa.text(
            "UPDATE test_suites SET data_source_id = :ds WHERE id = :sid"
        ), {"ds": agent_id, "sid": suite_id})
        homed += 1

    print(f"[evsuite0002] homed {homed} of {len(suites)} org-wide suites")


def downgrade() -> None:
    # Restores the post-evsuite0001 state: every suite org-wide.
    op.execute(sa.text("UPDATE test_suites SET data_source_id = NULL"))
