import re

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

_INTEGER_RANKS = {"tinyint": 0, "smallint": 1, "int": 2, "integer": 2, "bigint": 3}
_FLOAT_RANKS = {"float": 0, "real": 0, "double": 1}
_DECIMAL = re.compile(r"^(?:decimal|numeric|dec)(?:\((\d+)(?:,(\d+))?\))?$")
_TEXT = re.compile(r"^(varchar|varchar\(\d+\)|char\(\d+\))$")
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


def unify_glue_catalogs(per_tenant: dict[str, list[Table]]) -> UnifiedCatalog:
    catalog = unify_catalogs(per_tenant, GLUE_TYPE_RULES)
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
    return catalog
