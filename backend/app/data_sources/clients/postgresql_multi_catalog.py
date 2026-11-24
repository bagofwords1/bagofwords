import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from app.ai.prompt_formatters import ForeignKey, Table, TableColumn

TENANT_COLUMN = "tenant_db"
COLUMN_TYPES_KEY = "column_types"


@dataclass
class UnifiedCatalog:
    tables: list[Table] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)


def _schema_of(table: Table) -> str:
    return (table.metadata_json or {}).get("schema") or table.name.split(".", 1)[0]


# Type families DuckDB's UNION ALL BY NAME merges without loss: tenants whose
# column types differ only within one family are not a conflict. The unified
# column is advertised as the family's widest member. Anything else (numeric
# vs text, integer vs double, a timestamp with vs without time zone) still is.
# Numbers follow how DuckDB serves them: numeric(p,s) is an exact DECIMAL
# ("numeric"), bare numeric and numeric wider than 38 digits are DOUBLE
# ("float"). A DOUBLE in the same union would make every tenant's decimals
# approximate, so the two never merge.
_INTEGER_RANKS = {"smallint": 0, "int2": 0, "integer": 1, "int": 1, "int4": 1, "bigint": 2, "int8": 2}
_FLOAT_RANKS = {"real": 0, "float4": 0, "double precision": 1, "float8": 1}
_TEXT = re.compile(r"^(text|character varying|varchar|character|char|bpchar)(\s*\(\d+\))?$")
_NUMERIC = re.compile(r"^(numeric|decimal)(\s*\(\s*(\d+)\s*(,\s*\d+\s*)?\))?$")
# DuckDB's DECIMAL holds at most 38 digits; wider numerics are served as DOUBLE.
_MAX_EXACT_DIGITS = 38
_TEMPORAL = re.compile(r"^(timestamp|time)(\s*\(\d+\))?(\s+(without|with) time zone)?$")


def type_family(dtype: str | None) -> str:
    """The merge family of a Postgres type name as indexing reports it
    (`information_schema` names, or `format_type` with a modifier)."""
    t = " ".join((dtype or "").lower().split())
    if t in _INTEGER_RANKS:
        return "integer"
    if t in _FLOAT_RANKS:
        return "float"
    if _TEXT.match(t):
        return "text"
    numeric = _NUMERIC.match(t)
    if numeric:
        precision = numeric.group(3)
        return "numeric" if precision and int(precision) <= _MAX_EXACT_DIGITS else "float"
    match = _TEMPORAL.match(t)
    if match:
        zone = "with time zone" if match.group(4) == "with" else "without time zone"
        return f"{match.group(1)} {zone}"
    return t


def _family_type(family: str, dtypes: set[str]) -> str:
    """The type advertised for a column whose tenants' types are `dtypes`,
    all in `family`."""
    if len(dtypes) == 1:
        return next(iter(dtypes))
    normalized = {" ".join(d.lower().split()) for d in dtypes}
    if family == "integer":
        return max(normalized, key=lambda d: _INTEGER_RANKS[d])
    if family == "float":
        # Bare (or very wide) numeric is served as DOUBLE too; it ranks with it.
        return max(sorted(normalized), key=lambda d: _FLOAT_RANKS.get(d, 1))
    if family == "numeric":
        # Wide enough for every tenant's digits on both sides of the point,
        # as DuckDB's union widens DECIMALs; kept with a modifier so engine
        # builds still read it as exact.
        shapes = [_NUMERIC.match(d) for d in normalized]
        scales = [int((m.group(4) or ",0").strip(", ")) for m in shapes]
        whole = [int(m.group(3)) - scale for m, scale in zip(shapes, scales, strict=True)]
        return f"numeric({max(whole) + max(scales)},{max(scales)})"
    return family  # text, "timestamp without time zone", ...


@dataclass(frozen=True)
class TypeRules:
    family: Callable[[str | None], str]
    advertised: Callable[[str, set[str]], str]
    tenant_column_type: str = "text"


POSTGRES_TYPE_RULES = TypeRules(family=type_family, advertised=_family_type)


def _majority_types(members: dict[str, Table], rules: TypeRules) -> dict[str, tuple[str, str]]:
    """For each column, (majority family, advertised type)."""
    seen: dict[str, Counter] = {}
    spellings: dict[tuple[str, str], set[str]] = {}
    for tenant in sorted(members):
        for column in members[tenant].columns or []:
            family = rules.family(column.dtype)
            seen.setdefault(column.name, Counter())[family] += 1
            spellings.setdefault((column.name, family), set()).add(column.dtype or "")
    majority = {}
    for name, counts in seen.items():
        top = max(counts.values())
        family = min(f for f, count in counts.items() if count == top)
        majority[name] = (family, rules.advertised(family, spellings[(name, family)]))
    return majority


def _unify_table(
    name: str, members: dict[str, Table], excluded: list[dict], rules: TypeRules
) -> Table | None:
    expected = _majority_types(members, rules)
    included: list[str] = []
    dropped: list[str] = []
    for tenant in sorted(members):
        conflicts = [
            c for c in members[tenant].columns or [] if rules.family(c.dtype) != expected[c.name][0]
        ]
        if conflicts:
            dropped.append(tenant)
            for c in conflicts:
                excluded.append({
                    "table": name,
                    "tenant": tenant,
                    "column": c.name,
                    "type": c.dtype,
                    "expected_type": expected[c.name][1],
                })
        else:
            included.append(tenant)

    if not included:
        excluded.append({"table": name, "reason": "no tenant matches the resolved column types"})
        return None

    columns = [TableColumn(name=TENANT_COLUMN, dtype=rules.tenant_column_type)]
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
                columns.append(TableColumn(name=c.name, dtype=expected[c.name][1], description=c.description))
        for pk in source.pks or []:
            if pk.name not in pk_names:
                pk_names.add(pk.name)
                pks.append(TableColumn(name=pk.name, dtype=expected[pk.name][1] if pk.name in expected else pk.dtype))
        for fk in source.fks or []:
            edge = (fk.column.name, fk.references_name, fk.references_column.name)
            if edge not in edge_set:
                edge_set.add(edge)
                edges.append(edge)
            if fk.references_name not in referenced:
                referenced.append(fk.references_name)

    fks = [
        ForeignKey(
            column=TableColumn(name=col, dtype=expected[col][1] if col in expected else None),
            references_name=ref,
            references_column=TableColumn(name=ref_col, dtype=None),
        )
        for col, ref, ref_col in edges
    ]
    fks += [
        ForeignKey(
            column=TableColumn(name=TENANT_COLUMN, dtype=rules.tenant_column_type),
            references_name=ref,
            references_column=TableColumn(name=TENANT_COLUMN, dtype=rules.tenant_column_type),
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
            # What each column was when indexed; engine builds check tenants
            # still serve these (postgresql_multi_engine._leave_out_drifted).
            COLUMN_TYPES_KEY: {c.name: c.dtype for c in columns if c.name != TENANT_COLUMN and c.dtype},
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


def unify_catalogs(
    per_tenant: dict[str, list[Table]], rules: TypeRules = POSTGRES_TYPE_RULES
) -> UnifiedCatalog:
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
        unified = _unify_table(name, members, catalog.excluded, rules)
        if unified is not None:
            catalog.tables.append(unified)
    return catalog
