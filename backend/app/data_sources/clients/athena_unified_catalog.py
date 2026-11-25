import re
from functools import lru_cache

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError

from app.ai.prompt_formatters import Table
from app.data_sources.clients.postgresql_multi_catalog import (
    TENANT_COLUMN,
    TypeRules,
    UnifiedCatalog,
    unify_catalogs,
)

TENANTS_KEY = "tenants"
TENANT_CASTS_KEY = "tenant_casts"
TENANT_MISSING_KEY = "tenant_missing"
UNREADABLE_TENANTS_KEY = "unreadable_tenants"
EXCLUDED_TENANTS_KEY = "excluded_tenants"
UNSUPPORTED_TYPE_REASON = "unsupported column type"

_UNUSABLE_TYPES = {exp.DataType.Type.UNKNOWN, exp.DataType.Type.USERDEFINED}

_INTEGER_RANKS = {"tinyint": 0, "smallint": 1, "int": 2, "integer": 2, "bigint": 3}
_FLOAT_RANKS = {"float": 0, "real": 0, "double": 1}
_DECIMAL = re.compile(r"^(?:decimal|numeric|dec)(?:\((\d+)(?:,(\d+))?\))?$")
_TEXT = re.compile(r"^(varchar|varchar\(\d+\)|char\(\d+\))$")
_STRUCT_TOKENS = re.compile(r"\b(?:struct\s*<|row\s*\()|[<(]|[>)]|,", re.IGNORECASE)
_MAX_DECIMAL_DIGITS = 38
_DEFAULT_DECIMAL = (10, 0)


def _normalize(dtype: str | None) -> str:
    t = re.sub(r"\s*([(),<>])\s*", r"\1", " ".join((dtype or "").lower().split()))
    return "varchar" if t == "string" else t


def _decimal_shape(t: str) -> tuple[int, int] | None:
    match = _DECIMAL.match(t)
    if not match:
        return None
    if match.group(1) is None:
        return _DEFAULT_DECIMAL
    return int(match.group(1)), int(match.group(2) or 0)


def athena_type(dtype: str | None) -> exp.DataType | None:
    parsed = _parsed_athena_type(dtype or "")
    return parsed.copy() if parsed is not None else None


@lru_cache(maxsize=512)
def _parsed_athena_type(dtype: str) -> exp.DataType | None:
    try:
        parsed = sqlglot.parse_one(dtype, read="athena", into=exp.DataType, error_level=ErrorLevel.RAISE)
    except (SqlglotError, ValueError):
        return None
    if not isinstance(parsed, exp.DataType):
        return None
    if not all(_is_plain_type_node(node) for node in parsed.walk()):
        return None
    if _row_field_counts(parsed) != _source_struct_field_counts(dtype):
        return None
    return parsed


def _is_plain_type_node(node: exp.Expression) -> bool:
    if isinstance(node, exp.DataType):
        return node.this not in _UNUSABLE_TYPES and _has_its_type_params(node)
    if isinstance(node, exp.DataTypeParam):
        param = node.this
        return isinstance(param, exp.DataType) or (
            isinstance(param, exp.Literal) and param.is_number and param.name.isdigit()
        )
    if isinstance(node, exp.Literal):
        return isinstance(node.parent, exp.DataTypeParam)
    if isinstance(node, exp.Identifier):
        return isinstance(node.parent, exp.ColumnDef)
    return isinstance(node, exp.ColumnDef)


def _has_its_type_params(node: exp.DataType) -> bool:
    if node.this == exp.DataType.Type.ARRAY:
        return len(node.expressions) == 1
    if node.this == exp.DataType.Type.MAP:
        return len(node.expressions) == 2
    if node.this == exp.DataType.Type.STRUCT:
        return len(node.expressions) > 0
    return True


def _row_field_counts(parsed: exp.DataType) -> list[int]:
    return [
        len(node.expressions)
        for node in parsed.walk(bfs=False)
        if isinstance(node, exp.DataType) and node.this == exp.DataType.Type.STRUCT
    ]


def _source_struct_field_counts(dtype: str) -> list[int] | None:
    counts: list[int] = []
    open_frames: list[int | None] = []
    for match in _STRUCT_TOKENS.finditer(dtype):
        token = match.group(0)
        if token in (">", ")"):
            if not open_frames:
                return None
            open_frames.pop()
        elif token == ",":
            if open_frames and open_frames[-1] is not None:
                counts[open_frames[-1]] += 1
        elif token in ("<", "("):
            open_frames.append(None)
        else:
            open_frames.append(len(counts))
            counts.append(1)
    return counts if not open_frames else None


def glue_type_family(dtype: str | None) -> str:
    t = _normalize(dtype)
    if t in _INTEGER_RANKS:
        return "integer"
    if t in _FLOAT_RANKS:
        return "float"
    if _TEXT.match(t):
        return "text"
    shape = _decimal_shape(t)
    if shape:
        return "decimal" if shape[0] <= _MAX_DECIMAL_DIGITS else "float"
    return t


def _advertised_type(family: str, dtypes: set[str]) -> str:
    normalized = {_normalize(d) for d in dtypes}
    if family == "integer":
        return max(sorted(normalized), key=_INTEGER_RANKS.__getitem__)
    if family == "float":
        return "double"
    if family == "decimal":
        shapes = [_decimal_shape(d) for d in normalized]
        scale = max(s for _, s in shapes)
        digits = max(p - s for p, s in shapes) + scale
        return "double" if digits > _MAX_DECIMAL_DIGITS else f"decimal({digits},{scale})"
    if family == "text":
        return "varchar"
    return next(iter(normalized))


GLUE_TYPE_RULES = TypeRules(
    family=glue_type_family, advertised=_advertised_type, tenant_column_type="varchar"
)


def _without_unsupported_types(
    per_tenant: dict[str, list[Table]],
) -> tuple[dict[str, list[Table]], list[dict]]:
    usable: dict[str, list[Table]] = {}
    unsupported: list[dict] = []
    for tenant, tables in per_tenant.items():
        usable[tenant] = []
        for table in tables:
            bad = [c.name for c in table.columns or [] if athena_type(_normalize(c.dtype)) is None]
            if not bad:
                usable[tenant].append(table)
            unsupported += [
                {"table": table.name, "tenant": tenant, "column": name, "reason": UNSUPPORTED_TYPE_REASON}
                for name in bad
            ]
    return usable, unsupported


def unify_glue_catalogs(per_tenant: dict[str, list[Table]]) -> UnifiedCatalog:
    usable, unsupported = _without_unsupported_types(per_tenant)
    catalog = unify_catalogs(usable, GLUE_TYPE_RULES)
    left_out: dict[str, set[str]] = {}
    for entry in unsupported:
        left_out.setdefault(entry["table"], set()).add(entry["tenant"])
    sources = {tenant: {t.name: t for t in tables} for tenant, tables in per_tenant.items()}
    for unified in catalog.tables:
        metadata = unified.metadata_json
        metadata.pop("schema", None)
        advertised = {c.name: _normalize(c.dtype) for c in unified.columns if c.name != TENANT_COLUMN}
        casts: dict[str, dict[str, str]] = {}
        missing: dict[str, list[str]] = {}
        for tenant in metadata[TENANTS_KEY]:
            source = {c.name: c.dtype for c in sources[tenant][unified.name].columns or []}
            tenant_casts = {
                name: dtype for name, dtype in source.items() if _normalize(dtype) != advertised[name]
            }
            if tenant_casts:
                casts[tenant] = tenant_casts
            absent = [name for name in advertised if name not in source]
            if absent:
                missing[tenant] = absent
        metadata[TENANT_CASTS_KEY] = casts
        metadata[TENANT_MISSING_KEY] = missing
        if unified.name in left_out:
            metadata[EXCLUDED_TENANTS_KEY] = sorted(set(metadata[EXCLUDED_TENANTS_KEY]) | left_out[unified.name])
    catalog.excluded.extend(unsupported)
    return catalog
