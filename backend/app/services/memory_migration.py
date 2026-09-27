"""Legacy ``Membership.memory`` → ``memory_entries`` migration.

Runs from the Alembic revision (sync connection, SQLAlchemy Core only — no
ORM, so it works against whatever the schema is at that revision) and can be
re-run safely: a membership that already has ``source='migration'`` entries
is skipped, so running it twice never duplicates anything.

Each non-empty line/bullet becomes one ``preferences`` entry with
``source='migration'`` and no tags. Lines that read like business
definitions are migrated as-is (never auto-converted) and reported so an
admin can move them to instructions by hand.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import List

import sqlalchemy as sa

from app.services.memory_rules import legacy_lines, looks_like_rule

logger = logging.getLogger(__name__)


# Typed lightweight table so JSON binds correctly on both SQLite and Postgres
# (a raw '[]' string would be a varchar on Postgres).
_ENTRIES = sa.table(
    "memory_entries",
    sa.column("id", sa.String), sa.column("organization_id", sa.String), sa.column("user_id", sa.String),
    sa.column("seq", sa.Integer), sa.column("handle", sa.String), sa.column("section", sa.String),
    sa.column("text", sa.Text), sa.column("tags", sa.JSON), sa.column("source", sa.String),
    sa.column("seen_count", sa.Integer), sa.column("last_seen_at", sa.DateTime),
    sa.column("status", sa.String), sa.column("created_at", sa.DateTime), sa.column("updated_at", sa.DateTime),
)


@dataclass
class MigrationReport:
    memberships: int = 0
    entries: int = 0
    skipped_already_migrated: int = 0
    definition_like: List[dict] = field(default_factory=list)


def migrate_legacy_memory(connection, now: datetime | None = None) -> MigrationReport:
    now = now or datetime.utcnow()
    report = MigrationReport()
    rows = connection.execute(
        sa.text(
            "SELECT organization_id, user_id, memory FROM memberships "
            "WHERE user_id IS NOT NULL AND memory IS NOT NULL AND memory <> ''"
        )
    ).fetchall()
    for org_id, user_id, memory in rows:
        lines = legacy_lines(memory)
        if not lines:
            continue
        already = connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM memory_entries WHERE organization_id = :o AND user_id = :u "
                "AND source = 'migration'"
            ),
            {"o": str(org_id), "u": str(user_id)},
        ).scalar()
        if already:
            report.skipped_already_migrated += 1
            continue
        max_seq = connection.execute(
            sa.text("SELECT MAX(seq) FROM memory_entries WHERE organization_id = :o AND user_id = :u"),
            {"o": str(org_id), "u": str(user_id)},
        ).scalar() or 0
        report.memberships += 1
        for i, line in enumerate(lines, start=1):
            seq = int(max_seq) + i
            connection.execute(
                _ENTRIES.insert().values(
                    id=str(uuid.uuid4()), organization_id=str(org_id), user_id=str(user_id), seq=seq,
                    handle=f"m{seq}", section="preferences", text=line, tags=[], source="migration",
                    seen_count=1, last_seen_at=now, status="active", created_at=now, updated_at=now,
                )
            )
            report.entries += 1
            hit = looks_like_rule(line)
            if hit:
                report.definition_like.append({
                    "organization_id": str(org_id), "user_id": str(user_id), "handle": f"m{seq}",
                    "matched": hit, "text": line[:120],
                })
    for d in report.definition_like:
        # Operator-facing review list (the spec asks for these to be logged so
        # they can be moved to instructions by hand). Only definition-like
        # lines are logged; personal lines never are.
        logger.warning(
            "memory migration: entry %s (org %s, user %s) reads like a business definition — "
            "review and move it to instructions by hand: %r",
            d["handle"], d["organization_id"], d["user_id"], d["text"],
        )
    logger.info(
        "memory migration: %s memberships, %s entries, %s already migrated, %s definition-like",
        report.memberships, report.entries, report.skipped_already_migrated, len(report.definition_like),
    )
    return report
