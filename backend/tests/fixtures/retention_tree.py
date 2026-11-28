"""Test helpers for retention: fill every table of a report's tree, and count
what is left.

``seed_tree`` writes one row in **every** table a retention purge may
delete, hanging off one report (and one dashboard). It walks
``Base.metadata`` instead of naming tables, so a model added tomorrow is
seeded — and its purge checked — without touching the test. Required columns
get placeholder values; foreign keys point at the rows already seeded for
the tree, or at a row seeded on demand outside it (a data source, an
instruction, an eval suite…) — those must survive the purge.

``orphans`` checks every foreign key and logical reference of the schema:
a child row whose non-null reference names a row that does not exist. It
must be empty after any purge.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Enum, Float, Integer, Numeric, String, Text, func, insert, select
from sqlalchemy.types import TypeDecorator

from app.services.retention import purge


def _md():
    return purge._metadata()


def _placeholder(col):
    t = col.type
    impl = t.impl if isinstance(t, TypeDecorator) else t
    impl_cls = impl if isinstance(impl, type) else type(impl)
    py = None
    try:
        py = t.python_type
    except Exception:  # noqa: BLE001 — custom types without a python_type
        py = None
    if isinstance(t, Enum) and t.enums:
        return t.enums[0]
    if py is dict or issubclass(impl_cls, JSON):
        return {}
    if py is list:
        return []
    if py is bool or issubclass(impl_cls, Boolean):
        return False
    if py is int or issubclass(impl_cls, Integer):
        return 1
    if py is float or issubclass(impl_cls, (Float, Numeric)):
        return 1.0
    if py is datetime or issubclass(impl_cls, DateTime):
        return datetime.utcnow()
    if issubclass(impl_cls, (String, Text)):
        length = getattr(impl, "length", None) or 36
        return uuid.uuid4().hex[: min(length, 32)]
    return uuid.uuid4().hex[:16]


# Values some columns need to pass a CHECK constraint, and the states that
# make the tree "touched once, but nothing keeps it now": no live schedule,
# webhook, share, embed or publish, and no run in progress. Each of those
# keeps a report or dashboard; the exemption tests set them one at a time.
_NOW = datetime.utcnow()
OVERRIDES = {
    ("queries", "artifact_release_id"): None,
    ("visualizations", "artifact_release_id"): None,
    ("reports", "report_type"): "regular",
    ("artifacts", "published_release_id"): None,
    ("scheduled_prompts", "is_active"): False,
    ("webhooks", "is_active"): False,
    ("agent_executions", "status"): "success",
    ("report_shares", "deleted_at"): _NOW,
    ("artifact_shares", "deleted_at"): _NOW,
    ("artifact_host_applications", "deleted_at"): _NOW,
}


class TreeSeeder:
    """Seeds rows on demand; ``ids[table]`` is the row seeded for it."""

    def __init__(self, conn, *, org_id: str, user_id: str, fixed: Optional[dict] = None,
                 tables: Optional[set] = None):
        self.conn = conn
        self.ids: dict[str, str] = {"organizations": str(org_id), "users": str(user_id), **(fixed or {})}
        self.tables = tables if tables is not None else purge.deletable_tables()
        self.visiting: set[str] = set()

    async def ensure(self, name: str) -> Optional[str]:
        if name in self.ids:
            return self.ids[name]
        if name in self.visiting:
            return None
        self.visiting.add(name)
        try:
            table = _md().tables[name]
            values = {}
            fk_targets = {fk.parent.name: fk.column.table.name for fk in table.foreign_keys}
            for col in table.columns:
                key = (name, col.name)
                if key in OVERRIDES:
                    values[col.name] = OVERRIDES[key]
                    continue
                if col.name in fk_targets:
                    target = fk_targets[col.name]
                    if target == name:
                        continue  # self reference: leave empty
                    # In-tree references are always filled (the purge must
                    # follow them); outside ones only when required.
                    if not col.nullable or target in self.tables or target in self.ids:
                        target_id = await self.ensure(target)
                        if target_id is None and not col.nullable:
                            raise RuntimeError(f"cannot seed {name}.{col.name} -> {target}")
                        values[col.name] = target_id
                    continue
                if col.primary_key:
                    if not isinstance(col.type, Integer):
                        length = getattr(col.type, "length", None) or 36
                        values[col.name] = str(uuid.uuid4()) if length >= 36 else uuid.uuid4().hex[:length]
                    continue
                if col.name == "organization_id":
                    values[col.name] = self.ids["organizations"]
                    continue
                if col.name == "deleted_at":
                    continue
                if not col.nullable and col.default is None and col.server_default is None:
                    values[col.name] = _placeholder(col)
            # Logical references point into the tree too.
            for child, column, target in purge.LOGICAL_REFERENCES:
                if child == name and target in self.ids:
                    values[column] = self.ids[target]
            result = await self.conn.execute(insert(table).values(**values))
            if "id" in table.c:
                row_id = values.get("id")
                if row_id is None:
                    row_id = result.inserted_primary_key[0]
                self.ids[name] = str(row_id)
            else:
                self.ids[name] = "link"
            return self.ids[name]
        finally:
            self.visiting.discard(name)

    async def fill_logical_references(self):
        """Second pass: logical references whose target was seeded later."""
        from sqlalchemy import update

        md = _md()
        for child, column, target in purge.LOGICAL_REFERENCES:
            if child in self.ids and target in self.ids and "id" in md.tables[child].c:
                t = md.tables[child]
                await self.conn.execute(update(t).where(t.c.id == self.ids[child])
                                        .values({column: self.ids[target]}))


def _keep_side_tables(tree: set) -> set:
    out = set()
    for parent in tree:
        for ref in purge.references_into(parent):
            if ref.policy == purge.NULL and ref.child.name not in tree:
                out.add(ref.child.name)
    return out


async def seed_tree(db, *, org_id: str, user_id: str, report_id: str) -> dict[str, str]:
    """One row in every purgeable table, all belonging to ``report_id``
    (``db`` is an AsyncSession; committed here).

    The dashboard in the tree is an artifact of that report that was never
    published (the kind a conversation's deletion takes with it)."""
    seeder = TreeSeeder(db, org_id=org_id, user_id=user_id, fixed={"reports": str(report_id)})
    for name in sorted(seeder.tables):
        await seeder.ensure(name)
    # And every table that keeps a link into the tree (instructions, usage
    # records, eval cases…): those rows must survive with the link cleared.
    for name in sorted(_keep_side_tables(seeder.tables)):
        await seeder.ensure(name)
    await seeder.fill_logical_references()
    await db.commit()
    return dict(seeder.ids)


async def orphans(db) -> dict[str, int]:
    """``{"child.column -> parent": n}`` for every dangling reference."""
    md = _md()
    out: dict[str, int] = {}
    refs = []
    for table in md.tables.values():
        for fk in table.foreign_keys:
            refs.append((table, fk.parent.name, fk.column.table, fk.column.name))
    for child, column, parent in purge.LOGICAL_REFERENCES:
        if child in md.tables and parent in md.tables:
            refs.append((md.tables[child], column, md.tables[parent], "id"))
    for child, column, parent, parent_col in refs:
        c = child.c[column]
        # Aliased: for a self-reference (completions.parent_id) the parent
        # must be another row, not the outer one correlated back in.
        p = parent.alias().c[parent_col]
        stmt = select(func.count()).select_from(child).where(
            c.isnot(None), ~select(p).where(p == c).exists())
        n = (await db.execute(stmt)).scalar()
        if n:
            out[f"{child.name}.{column} -> {parent.name}"] = n
    return out


async def surviving(db, ids: dict[str, str]) -> dict[str, bool]:
    """For each seeded table with an id: does its row still exist?"""
    md = _md()
    out = {}
    for name, row_id in ids.items():
        t = md.tables.get(name)
        if t is None or "id" not in t.c or row_id == "link":
            continue
        out[name] = bool((await db.execute(select(func.count()).select_from(t).where(t.c.id == row_id))).scalar())
    return out
