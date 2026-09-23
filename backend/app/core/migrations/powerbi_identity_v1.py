"""Frozen, model-independent catalog repair shared with the pbiidentity01 migration.

Do not change the schema assumptions here after release; add a new version.
The caller owns the transaction and must serialize writers for the connection.
"""
from collections import defaultdict

import sqlalchemy as sa


def repair_powerbi_identities(bind, connection_id):
    def table(name, *columns):
        return sa.table(name, *[sa.column(c, t) for c, t in columns])

    ct = table("connection_tables", ("id", sa.String), ("connection_id", sa.String),
               ("kind", sa.String), ("name", sa.String), ("metadata_json", sa.JSON),
               ("created_at", sa.DateTime))
    dt = table("datasource_tables", ("id", sa.String), ("connection_table_id", sa.String),
               ("datasource_id", sa.String), ("is_active", sa.Boolean), ("created_at", sa.DateTime))
    uct = table("user_connection_tables", ("connection_table_id", sa.String))
    refs = [
        ("user_data_source_tables", "data_source_table_id"),
        ("table_stats", "datasource_table_id"),
        ("table_usage_events", "datasource_table_id"),
        ("table_feedback_events", "datasource_table_id"),
    ]
    groups = defaultdict(list)
    for row in bind.execute(sa.select(ct).where(ct.c.connection_id == connection_id, ct.c.kind == "table")).mappings():
        meta = row["metadata_json"]
        pbi = meta.get("powerbi") if isinstance(meta, dict) else None
        if isinstance(pbi, dict) and pbi.get("datasetId") and pbi.get("tableName"):
            groups[(str(pbi.get("workspaceId") or ""), str(pbi["datasetId"]), str(pbi["tableName"]))].append(row)
    repaired = 0
    for rows in groups.values():
        if len(rows) < 2:
            continue
        # Keep the oldest canonical ID, with a deterministic tie-breaker.
        rows.sort(key=lambda r: (r["created_at"] is None, str(r["created_at"] or ""), r["id"]))
        target = rows[0]["id"]
        # One predicate per row avoids a bind-limit failure on large catalogs.
        domains = []
        for row in rows:
            domains.extend(bind.execute(sa.select(dt).where(dt.c.connection_table_id == row["id"])).mappings())
        by_domain = defaultdict(list)
        for domain in domains:
            by_domain[domain["datasource_id"]].append(domain)
        for domain_rows in by_domain.values():
            # Prefer an already-selected row so report/overlay IDs stay stable.
            domain_rows.sort(key=lambda r: (not r["is_active"], r["created_at"] is None,
                                            str(r["created_at"] or ""), r["id"]))
            survivor = domain_rows[0]["id"]
            bind.execute(sa.update(dt).where(dt.c.id == survivor).values(connection_table_id=target))
            for duplicate in domain_rows[1:]:
                for name, column in refs:
                    ref = table(name, (column, sa.String))
                    bind.execute(sa.update(ref).where(ref.c[column] == duplicate["id"]).values({column: survivor}))
                bind.execute(sa.delete(dt).where(dt.c.id == duplicate["id"]))
        for duplicate in rows[1:]:
            # Preserve each user's status, name and column grants; only relink.
            bind.execute(sa.update(uct).where(uct.c.connection_table_id == duplicate["id"])
                         .values(connection_table_id=target))
            bind.execute(sa.delete(ct).where(ct.c.id == duplicate["id"]))
            repaired += 1
    return repaired
