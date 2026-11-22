from collections import Counter
from dataclasses import dataclass, field

from app.ai.prompt_formatters import ForeignKey, Table, TableColumn

TENANT_COLUMN = "tenant_db"


@dataclass
class UnifiedCatalog:
    tables: list[Table] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)


def _schema_of(table: Table) -> str:
    return (table.metadata_json or {}).get("schema") or table.name.split(".", 1)[0]


def _majority_types(members: dict[str, Table]) -> dict[str, str]:
    seen: dict[str, Counter] = {}
    for tenant in sorted(members):
        for column in members[tenant].columns or []:
            seen.setdefault(column.name, Counter())[column.dtype] += 1
    majority = {}
    for name, counts in seen.items():
        top = max(counts.values())
        majority[name] = min((dtype for dtype, count in counts.items() if count == top), key=lambda d: d or "")
    return majority


def _unify_table(name: str, members: dict[str, Table], excluded: list[dict]) -> Table | None:
    expected = _majority_types(members)
    included: list[str] = []
    dropped: list[str] = []
    for tenant in sorted(members):
        conflicts = [
            c for c in members[tenant].columns or [] if c.dtype != expected[c.name]
        ]
        if conflicts:
            dropped.append(tenant)
            for c in conflicts:
                excluded.append({
                    "table": name,
                    "tenant": tenant,
                    "column": c.name,
                    "type": c.dtype,
                    "expected_type": expected[c.name],
                })
        else:
            included.append(tenant)

    if not included:
        excluded.append({"table": name, "reason": "no tenant matches the resolved column types"})
        return None

    columns = [TableColumn(name=TENANT_COLUMN, dtype="text")]
    column_names = {TENANT_COLUMN}
    pks: list[TableColumn] = []
    pk_names: set[str] = set()
    edges: list[tuple[str, str, str]] = []
    edge_set: set[tuple[str, str, str]] = set()
    referenced: list[str] = []
    description = None

    for tenant in included:
        source = members[tenant]
        description = description or source.description or None
        for c in source.columns or []:
            if c.name not in column_names:
                column_names.add(c.name)
                columns.append(TableColumn(name=c.name, dtype=expected[c.name], description=c.description))
        for pk in source.pks or []:
            if pk.name not in pk_names:
                pk_names.add(pk.name)
                pks.append(TableColumn(name=pk.name, dtype=expected.get(pk.name, pk.dtype)))
        for fk in source.fks or []:
            edge = (fk.column.name, fk.references_name, fk.references_column.name)
            if edge not in edge_set:
                edge_set.add(edge)
                edges.append(edge)
            if fk.references_name not in referenced:
                referenced.append(fk.references_name)

    fks = [
        ForeignKey(
            column=TableColumn(name=col, dtype=expected.get(col)),
            references_name=ref,
            references_column=TableColumn(name=ref_col, dtype=None),
        )
        for col, ref, ref_col in edges
    ]
    fks += [
        ForeignKey(
            column=TableColumn(name=TENANT_COLUMN, dtype="text"),
            references_name=ref,
            references_column=TableColumn(name=TENANT_COLUMN, dtype="text"),
        )
        for ref in referenced
    ]

    return Table(
        name=name,
        description=description,
        columns=columns,
        pks=pks,
        fks=fks,
        metadata_json={
            "schema": _schema_of(next(iter(members.values()))),
            "tenants": included,
            "excluded_tenants": dropped,
        },
    )


def _column_case_collisions(members: dict[str, Table]) -> list[list[str]]:
    """Groups of column spellings that differ only by case, across every
    tenant's copy of one table (and within one copy)."""
    spellings: dict[str, set[str]] = {}
    for table in members.values():
        for column in table.columns or []:
            name = column.name or ""
            spellings.setdefault(name.casefold(), set()).add(name)
    return [sorted(names) for _, names in sorted(spellings.items()) if len(names) > 1]


def unify_catalogs(per_tenant: dict[str, list[Table]]) -> UnifiedCatalog:
    groups: dict[str, dict[str, Table]] = {}
    for tenant in sorted(per_tenant):
        for table in per_tenant[tenant]:
            groups.setdefault(table.name, {})[tenant] = table

    folded: dict[str, list[str]] = {}
    for name in groups:
        folded.setdefault(name.casefold(), []).append(name)

    catalog = UnifiedCatalog()
    for name in sorted(groups):
        members = groups[name]
        twins = [other for other in folded[name.casefold()] if other != name]
        if twins:
            catalog.excluded.append({"table": name, "reason": "name collides by case", "with": sorted(twins)})
            continue
        if any(
            TENANT_COLUMN in {(c.name or "").casefold() for c in t.columns or []} for t in members.values()
        ):
            catalog.excluded.append({"table": name, "reason": "has tenant_db column"})
            continue
        column_twins = _column_case_collisions(members)
        if column_twins:
            # DuckDB matches column names case-insensitively: UNION ALL BY NAME
            # would merge `Amount` and `amount` into one column of both
            # tenants' values while the catalog advertised two.
            catalog.excluded.append({
                "table": name,
                "reason": "column names collide by case",
                "columns": column_twins,
            })
            continue
        unified = _unify_table(name, members, catalog.excluded)
        if unified is not None:
            catalog.tables.append(unified)
    return catalog
