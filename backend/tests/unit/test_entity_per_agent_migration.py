"""What the entagent01 backfill and downgrade do to existing saved queries.

The backfill uses the live classification (app/services/entity_code.py), so
this pins its effect on stored rows: which queries become agent-free, which
are left exactly as they were, and that the downgrade gives the pre-migration
code back.
"""
import uuid

import sqlalchemy as sa

from app.core.migrations.entity_per_agent_v1 import backfill, restore_code

SCHEMA = [
    "CREATE TABLE data_sources (id TEXT PRIMARY KEY, name TEXT, organization_id TEXT, created_at TEXT)",
    "CREATE TABLE connections (id TEXT PRIMARY KEY, name TEXT, type TEXT, is_active BOOLEAN, created_at TEXT)",
    "CREATE TABLE domain_connection (data_source_id TEXT, connection_id TEXT)",
    "CREATE TABLE entities (id TEXT PRIMARY KEY, organization_id TEXT, code TEXT, code_mode TEXT, origin_data_source_id TEXT)",
    "CREATE TABLE entity_data_source_association (entity_id TEXT, data_source_id TEXT)",
    "CREATE TABLE entity_user_results (entity_id TEXT, data_source_id TEXT)",
    "CREATE TABLE audit_logs (id TEXT, created_at TEXT, organization_id TEXT, user_id TEXT, action TEXT, "
    "resource_type TEXT, resource_id TEXT, details TEXT)",
]


def _code(key):
    return f"def generate_df(ds_clients, excel_files):\n    return ds_clients[{key!r}].execute_query('SELECT 1')\n"


class _World:
    def __init__(self):
        self.engine = sa.create_engine("sqlite://")
        self.conn = self.engine.connect()
        for stmt in SCHEMA:
            self.conn.execute(sa.text(stmt))
        self.org = str(uuid.uuid4())

    def agent(self, name, *conns):
        ds = str(uuid.uuid4())
        self.conn.execute(sa.text("INSERT INTO data_sources VALUES (:i, :n, :o, :t)"),
                          {"i": ds, "n": name, "o": self.org, "t": f"2026-01-0{len(name) % 9 + 1}"})
        for cname, ctype, active in conns:
            cid = str(uuid.uuid4())
            self.conn.execute(sa.text("INSERT INTO connections VALUES (:i, :n, :t, :a, '2026-01-01')"),
                              {"i": cid, "n": cname, "t": ctype, "a": active})
            self.conn.execute(sa.text("INSERT INTO domain_connection VALUES (:d, :c)"), {"d": ds, "c": cid})
        return ds

    def query(self, code, *agents):
        eid = str(uuid.uuid4())
        self.conn.execute(sa.text("INSERT INTO entities (id, organization_id, code) VALUES (:i, :o, :c)"),
                          {"i": eid, "o": self.org, "c": code})
        for ds in agents:
            self.conn.execute(sa.text("INSERT INTO entity_data_source_association VALUES (:e, :d)"), {"e": eid, "d": ds})
        return eid

    def row(self, eid):
        return self.conn.execute(sa.text("SELECT code, code_mode FROM entities WHERE id = :i"), {"i": eid}).one()


def test_backfill_and_downgrade_keep_every_query_runnable_on_the_connection_it_named():
    w = _World()
    plain = w.agent("plain", ("main", "postgresql", True))
    down = w.agent("down", ("only", "postgresql", False))
    sales = w.agent("sales", ("prod", "postgresql", False), ("staging", "postgresql", True))
    other = w.agent("other", ("main", "postgresql", True))

    templated = w.query(_code("plain:main"), plain)
    inactive_only = w.query(_code("down:only"), down)
    pinned = w.query(_code("sales:prod"), sales)
    names_living_agent = w.query(_code("other:main"), plain)

    backfill(w.conn)

    assert w.row(templated) == (_code("$agent:postgresql"), "templated")
    assert w.row(inactive_only) == (_code("$agent:postgresql"), "templated")
    # prod is inactive beside an active staging: never moved onto staging.
    assert w.row(pinned).code == _code("sales:prod")
    assert w.row(pinned).code_mode == "bound"
    # A key of an agent that exists but is not on the query is not repaired.
    assert w.row(names_living_agent) == (_code("other:main"), "unresolved")

    restore_code(w.conn)

    assert w.row(templated).code == _code("plain:main")
    # Its only connection is inactive: the downgrade still names it.
    assert w.row(inactive_only).code == _code("down:only")
    assert w.row(pinned).code == _code("sales:prod")
