"""Legacy ``Membership.memory`` → facts in ``memory_entries`` and rules in the
user's personal instructions.

Runs from the Alembic revision (sync connection, SQLAlchemy Core only — no
ORM, so it works against whatever the schema is at that revision) and can be
re-run safely: a membership that already has ``source='migration'`` entries,
or whose rule lines are already in its note, is not migrated twice.

The old document mixed two things. Each non-empty line/bullet is sorted by
the same test the agent uses (``memory_rules.looks_like_rule``):

- a fact about the user → one memory entry (``source='migration'``, no tags);
- a rule about how to answer or compute → appended to ``memberships.note``
  (the user's Custom instructions), while it fits the note's cap. Rules that
  don't fit stay only in the read-only legacy column and are logged.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import List

import sqlalchemy as sa

from app.services.memory_rules import append_rule_to_note, legacy_lines, looks_like_rule

logger = logging.getLogger(__name__)

# Mirrors MEMBERSHIP_NOTE_MAX_LENGTH (kept literal: migrations must not import
# schema modules that may change after this revision).
NOTE_MAX_CHARS = 500

# Typed lightweight table so JSON binds correctly on both SQLite and Postgres
# (a raw '[]' string would be a varchar on Postgres).
_ENTRIES = sa.table(
    "memory_entries",
    sa.column("id", sa.String), sa.column("organization_id", sa.String), sa.column("user_id", sa.String),
    sa.column("seq", sa.Integer), sa.column("handle", sa.String),
    sa.column("text", sa.Text), sa.column("tags", sa.JSON), sa.column("source", sa.String),
    sa.column("seen_count", sa.Integer), sa.column("last_seen_at", sa.DateTime),
    sa.column("status", sa.String), sa.column("created_at", sa.DateTime), sa.column("updated_at", sa.DateTime),
)


@dataclass
class MigrationReport:
    memberships: int = 0
    entries: int = 0
    rules_to_instructions: int = 0
    skipped_already_migrated: int = 0
    rules_not_moved: List[dict] = field(default_factory=list)


def _append_to_note(note: str | None, rules: List[str]) -> tuple[str | None, List[str], List[str]]:
    """Append rule lines to a note as bullets while they fit. Returns (new
    note, moved, left_over). Lines already in the note count as moved."""
    current = (note or "").strip() or None
    moved: List[str] = []
    left: List[str] = []
    for rule in rules:
        updated = append_rule_to_note(current, rule, NOTE_MAX_CHARS)
        if updated is None:
            left.append(rule)
        else:
            current, _ = updated, moved.append(rule)
    return current, moved, left


def migrate_legacy_memory(connection, now: datetime | None = None) -> MigrationReport:
    now = now or datetime.utcnow()
    report = MigrationReport()
    rows = connection.execute(
        sa.text(
            "SELECT organization_id, user_id, memory, note FROM memberships "
            "WHERE user_id IS NOT NULL AND memory IS NOT NULL AND memory <> ''"
        )
    ).fetchall()
    for org_id, user_id, memory, note in rows:
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
        facts = [l for l in lines if not looks_like_rule(l)]
        rules = [l for l in lines if looks_like_rule(l)]
        new_note, moved, left = _append_to_note(note, rules)
        note_unchanged = new_note == ((note or "").strip() or None)
        # Already migrated: facts were inserted before, or there are no facts
        # and the note would not change (rules already there or no room left).
        if already or (not facts and note_unchanged):
            report.skipped_already_migrated += 1
            continue
        report.memberships += 1

        if new_note != ((note or "").strip() or None):
            connection.execute(
                sa.text("UPDATE memberships SET note = :n WHERE organization_id = :o AND user_id = :u"),
                {"n": new_note, "o": str(org_id), "u": str(user_id)},
            )
        report.rules_to_instructions += len(moved)
        for rule in left:
            report.rules_not_moved.append(
                {"organization_id": str(org_id), "user_id": str(user_id), "text": rule[:120]}
            )

        max_seq = connection.execute(
            sa.text("SELECT MAX(seq) FROM memory_entries WHERE organization_id = :o AND user_id = :u"),
            {"o": str(org_id), "u": str(user_id)},
        ).scalar() or 0
        for i, line in enumerate(facts, start=1):
            seq = int(max_seq) + i
            connection.execute(
                _ENTRIES.insert().values(
                    id=str(uuid.uuid4()), organization_id=str(org_id), user_id=str(user_id), seq=seq,
                    handle=f"m{seq}", text=line, tags=[], source="migration",
                    seen_count=1, last_seen_at=now, status="active", created_at=now, updated_at=now,
                )
            )
            report.entries += 1
    for d in report.rules_not_moved:
        # Operator-facing: the user's Custom instructions were full, so this
        # rule stays only in the read-only legacy column.
        logger.warning(
            "memory migration: rule for user %s (org %s) did not fit their custom instructions and was "
            "not moved — review by hand: %r",
            d["user_id"], d["organization_id"], d["text"],
        )
    logger.info(
        "memory migration: %s memberships, %s facts → memory, %s rules → custom instructions, "
        "%s already migrated, %s rules not moved",
        report.memberships, report.entries, report.rules_to_instructions,
        report.skipped_already_migrated, len(report.rules_not_moved),
    )
    return report
