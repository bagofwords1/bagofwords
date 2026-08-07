"""Relabel OIDC-synced groups whose name is just their raw directory GUID

Revision ID: oidcgrp01
Revises: evsuite0002
Create Date: 2026-08-07

Group sync writes one BOW ``Group`` per id in the OIDC ``groups`` claim, and
asks Graph for a display name. Not every id resolves: directory roles and
administrative units arrive through the same claim, and a tenant can withhold
individual groups from the app registration — ``getByIds`` simply omits those.

The Graph helper used to backfill each unresolved id with the id itself, so an
unreadable object was indistinguishable from a real name, and the group landed
in the admin's list as a bare ``85f43b45-99ae-43a0-a780-a05c119e8b9c`` sitting
next to properly named ones. The sync path now labels unresolved ids, but rows
already written keep the GUID until the member next signs in. This relabels
them in place so the existing list reads correctly right away.

Deliberately narrow — a row is touched only when all three hold:

  - it is OIDC-synced (``external_provider = 'oidc'``),
  - its ``name`` is *exactly* its ``external_id`` (so a group genuinely renamed
    by an admin, or one Graph resolved, is never rewritten), and
  - that id is GUID-shaped (providers that emit readable claim values — Okta,
    Keycloak, Entra configured for sAMAccountName — are left alone; there the
    name and the id being equal is correct).

Data-only and idempotent: a relabelled row no longer matches the predicate.
Downgrade restores the GUID name for exactly the rows this touched.
"""
import re

from alembic import op
import sqlalchemy as sa


revision = 'oidcgrp01'
down_revision = 'evsuite0002'
branch_labels = None
depends_on = None

_GUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)

_LABEL_RE = re.compile(
    r"^Unresolved directory group \([0-9a-f]{8}(?:…|-[0-9a-f-]+)\)$", re.IGNORECASE
)


def _guid_named_rows(bind):
    """OIDC groups still named by their own GUID."""
    rows = bind.execute(sa.text(
        "SELECT id, organization_id, external_id FROM groups "
        "WHERE external_provider = 'oidc' AND external_id IS NOT NULL "
        "AND name = external_id"
    )).fetchall()
    return [r for r in rows if _GUID_RE.match(r[2] or "")]


def upgrade() -> None:
    bind = op.get_bind()

    # groups carries UNIQUE (organization_id, name). The label keeps only the
    # first 8 hex digits, so two unresolved ids sharing that prefix would map to
    # one name — vanishingly unlikely, but a collision here fails the upgrade for
    # the whole deployment. Track names per org and spell the id out in full on
    # the second one; external_id is unique, so that always resolves.
    taken = {}
    for org_id, name in bind.execute(
        sa.text("SELECT organization_id, name FROM groups")
    ).fetchall():
        taken.setdefault(org_id, set()).add(name)

    for group_id, org_id, external_id in _guid_named_rows(bind):
        org_names = taken.setdefault(org_id, set())
        label = f"Unresolved directory group ({external_id[:8]}…)"
        if label in org_names:
            label = f"Unresolved directory group ({external_id})"
        if label in org_names:
            continue  # already relabelled under this exact name; nothing to do
        org_names.discard(external_id)
        org_names.add(label)
        bind.execute(
            sa.text("UPDATE groups SET name = :name WHERE id = :id"),
            {"name": label, "id": group_id},
        )


def downgrade() -> None:
    bind = op.get_bind()

    rows = bind.execute(sa.text(
        "SELECT id, external_id, name FROM groups "
        "WHERE external_provider = 'oidc' AND external_id IS NOT NULL"
    )).fetchall()

    for group_id, external_id, name in rows:
        if not _GUID_RE.match(external_id or "") or not _LABEL_RE.match(name or ""):
            continue
        if name not in (
            f"Unresolved directory group ({external_id[:8]}…)",
            f"Unresolved directory group ({external_id})",
        ):
            continue  # labelled for a different id — not a row this migration wrote
        bind.execute(
            sa.text("UPDATE groups SET name = :name WHERE id = :id"),
            {"name": external_id, "id": group_id},
        )
