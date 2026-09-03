"""Query parameter resolution, safe SQL rendering, and code checks.

This is the single audited place where parameter VALUES meet SQL TEXT.
Generated code calls `execute_query(sql, params={...})` with `:name`
placeholders; the QueryCapturingClientWrapper pops `params` and calls
`render_sql_with_params` here, then hands the finished plain SQL to the
untouched client. LLM code never assembles the final string — the AST
validator (`check_params_not_formatted`) enforces that `params` values are
never pushed through f-strings/format/%.

Rendering is safe-literal (typed quoting/escaping), not driver binds: values
only, never identifiers, with per-dialect string escaping. Native driver
binds can later be added per-client behind the same wrapper without touching
this contract.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from app.schemas.param_schema import (
    IDENTITY_ATTR_PREFIX,
    ParamSpec,
)


class ParamError(ValueError):
    """A parameter failed validation/rendering. Surfaced as HTTP 400."""


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

def params_fingerprint(values: Optional[Dict[str, Any]]) -> str:
    """Stable hash of resolved param values. '' when there are none, so the
    no-params fast path keeps the pre-params StepUserResult key shape."""
    if not values:
        return ""
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Value validation / coercion
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")
_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _coerce_scalar(spec: ParamSpec, value: Any) -> Any:
    t = spec.type
    if value is None:
        return None
    if t == "number":
        if isinstance(value, bool):
            raise ParamError(f"param '{spec.name}': expected number, got boolean")
        if isinstance(value, (int, float)):
            return value
        s = str(value).strip()
        if not _NUM_RE.match(s):
            raise ParamError(f"param '{spec.name}': '{value}' is not a number")
        return float(s) if "." in s else int(s)
    if t == "date":
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        s = str(value).strip()
        if not _DATE_RE.match(s):
            raise ParamError(f"param '{spec.name}': '{value}' is not an ISO date")
        return s
    if t in ("string", "id"):
        if isinstance(value, (dict, list)):
            raise ParamError(f"param '{spec.name}': expected a scalar {t}")
        return str(value)
    return value


def coerce_param_value(spec: ParamSpec, value: Any) -> Any:
    """Validate + normalize one submitted value against its declaration."""
    if value is None:
        if spec.required and spec.default is None:
            raise ParamError(f"param '{spec.name}' is required")
        return None
    if spec.type == "list":
        items = value if isinstance(value, list) else [value]
        return [
            _coerce_scalar(ParamSpec(name=spec.name, type="string"), v)
            for v in items
        ]
    if spec.type == "date_range":
        if not isinstance(value, dict) or not ("from" in value or "to" in value):
            raise ParamError(
                f"param '{spec.name}': date_range must be an object with 'from'/'to'"
            )
        out = {}
        for k in ("from", "to"):
            if value.get(k) is not None:
                out[k] = _coerce_scalar(ParamSpec(name=spec.name, type="date"), value[k])
        return out or None
    coerced = _coerce_scalar(spec, value)
    if spec.strict_options and spec.options:
        allowed = {str(o) for o in spec.options}
        if str(coerced) not in allowed:
            raise ParamError(f"param '{spec.name}': value not in allowed options")
    return coerced


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------

def resolve_identity_binding(binding: str, identity) -> Any:
    """Map a binding expression to a value on a resolved Identity
    (app.data_sources.fast.rls.Identity — full resolution, never the capped
    display payload). Returns None when the identity lacks the attribute."""
    if identity is None:
        return None
    if binding == "viewer.email":
        return getattr(identity, "email", None)
    if binding == "viewer.user_id":
        return getattr(identity, "user_id", None)
    if binding == "viewer.name":
        return getattr(identity, "name", None) or getattr(identity, "email", None)
    if binding == "viewer.groups":
        groups = getattr(identity, "group_names", None)
        if groups is None:
            groups = getattr(identity, "groups", None)
        # Sorted for a stable params fingerprint (group_names is a set).
        return sorted(groups) if groups else []
    if binding.startswith(IDENTITY_ATTR_PREFIX):
        key = binding[len(IDENTITY_ATTR_PREFIX):]
        attrs = getattr(identity, "profile_attributes", None) or {}
        return attrs.get(key)
    return None


def resolve_param_values(
    specs: List[ParamSpec],
    request_values: Optional[Dict[str, Any]],
    identity=None,
    *,
    reject_unknown: bool = True,
) -> Dict[str, Any]:
    """Merge defaults ← request values ← identity bindings into the dict the
    code receives.

    - Unknown request keys are rejected (defense against typo'd/probing input).
    - identity source: client values are REJECTED — the server-resolved
      identity value always wins (an unauthenticated/anonymous identity
      resolves to None, which the code treats as "no rows" per its own guard).
    - input_identity_default: identity value used only when the client sent
      nothing.
    """
    request_values = dict(request_values or {})
    by_name = {s.name: s for s in specs}

    if reject_unknown:
        unknown = [k for k in request_values.keys() if k not in by_name]
        if unknown:
            raise ParamError(f"unknown parameter(s): {', '.join(sorted(unknown))}")

    resolved: Dict[str, Any] = {}
    for spec in specs:
        if spec.source == "identity":
            if spec.name in request_values:
                raise ParamError(
                    f"param '{spec.name}' is identity-locked and cannot be set by the client"
                )
            value = resolve_identity_binding(spec.identity_binding or "viewer.email", identity)
        elif spec.source == "input_identity_default":
            if spec.name in request_values and request_values[spec.name] is not None:
                value = coerce_param_value(spec, request_values[spec.name])
            else:
                value = resolve_identity_binding(spec.identity_binding or "viewer.email", identity)
        else:
            if spec.name in request_values:
                value = coerce_param_value(spec, request_values[spec.name])
            else:
                value = coerce_param_value(spec, spec.default)
        if value is None and spec.required:
            raise ParamError(f"param '{spec.name}' is required")
        resolved[spec.name] = value
    return resolved


# ---------------------------------------------------------------------------
# Safe SQL rendering
# ---------------------------------------------------------------------------

# Clients whose string literals treat backslash as an escape character by
# default; both ' and \ must be escaped there.
_BACKSLASH_DIALECT_CLIENTS = {"MysqlClient", "MariadbClient", "MariaDBClient"}

# T-SQL clients: a plain 'literal' is a varchar in the database's default
# collation, so any character outside that code page is silently replaced by
# '?' at parse time (a Hebrew value on a SQL_Latin1_General database becomes
# '???' and matches nothing). Non-ASCII strings must be N'literal' (nvarchar)
# there. ASCII stays plain so comparisons against varchar columns keep their
# type and index behaviour.
_UNICODE_PREFIX_CLIENTS = {"MSSQLClient", "MsFabricClient"}

_PLACEHOLDER_RE = re.compile(r"(?<![:\w]):([a-zA-Z_][a-zA-Z0-9_]*)")


def _render_literal(
    value: Any, *, backslash_dialect: bool, unicode_prefix: bool = False
) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        # Guard inf/nan from reaching SQL text.
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise ParamError("non-finite number cannot be rendered into SQL")
        return repr(value)
    if isinstance(value, (date, datetime)):
        value = value.isoformat()
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        if not items:
            # Empty IN () is invalid SQL nearly everywhere; render a
            # never-matching list instead so "no groups" means no rows.
            return "(NULL)"
        return "(" + ", ".join(
            _render_literal(
                v, backslash_dialect=backslash_dialect, unicode_prefix=unicode_prefix
            )
            for v in items
        ) + ")"
    if isinstance(value, dict):
        raise ParamError("object values cannot be rendered into SQL directly")
    s = str(value)
    s = s.replace("'", "''")
    if backslash_dialect:
        s = s.replace("\\", "\\\\")
    prefix = "N" if (unicode_prefix and not s.isascii()) else ""
    return prefix + "'" + s + "'"


def render_sql_with_params(
    sql: str,
    values: Dict[str, Any],
    *,
    client_type_name: str = "",
) -> str:
    """Substitute `:name` placeholders in `sql` with safely rendered literals.

    Every placeholder must have a value in `values` (missing → ParamError).
    Extra values are fine — a multi-query generate_df doesn't use every param
    in every statement. Values only, never identifiers: the placeholder must
    not be quoted or backticked context-sensitively — we replace the token
    wherever it appears as `:name`.
    """
    backslash = client_type_name in _BACKSLASH_DIALECT_CLIENTS
    unicode_prefix = client_type_name in _UNICODE_PREFIX_CLIENTS

    missing: List[str] = []

    def _sub(m: "re.Match[str]") -> str:
        name = m.group(1)
        if name not in values:
            missing.append(name)
            return m.group(0)
        return _render_literal(
            values[name], backslash_dialect=backslash, unicode_prefix=unicode_prefix
        )

    rendered = _PLACEHOLDER_RE.sub(_sub, sql)
    if missing:
        raise ParamError(
            "SQL uses parameter placeholder(s) with no value: " + ", ".join(sorted(set(missing)))
        )
    return rendered


# ---------------------------------------------------------------------------
# Code checks
# ---------------------------------------------------------------------------

def extract_code_param_names(code: str) -> set:
    """Names the code reads off the `params` dict: params["x"], params.get("x")."""
    names = set()
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        # params["x"]
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) \
                and node.value.id == "params":
            sl = node.slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                names.add(sl.value)
        # params.get("x", ...)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "get" and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "params":
            if node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                names.add(node.args[0].value)
    return names


def code_accepts_params(code: str) -> bool:
    """Does generate_df declare a `params` parameter?"""
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "generate_df":
            all_args = node.args.args + node.args.kwonlyargs
            return any(a.arg == "params" for a in all_args)
    return False


def check_params_not_formatted(code: str) -> Optional[str]:
    """Best-effort AST guard: `params` must never be pushed through string
    formatting (f-string, .format, %). Returns a message on violation, None
    when clean. Defense-in-depth — the rendering wrapper is the real
    enforcement."""
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return None

    def _mentions_params(node: ast.AST) -> bool:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id == "params":
                return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for v in node.values:
                if isinstance(v, ast.FormattedValue) and _mentions_params(v.value):
                    return "param values must not be interpolated into f-strings — pass them via execute_query(sql, params=...)"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "format":
            if any(_mentions_params(a) for a in list(node.args) + [kw.value for kw in node.keywords]):
                return "param values must not be passed through .format() — use execute_query(sql, params=...)"
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            if _mentions_params(node.right):
                return "param values must not be %-formatted into strings — use execute_query(sql, params=...)"
    return None


def check_declarations_vs_code(code: str, specs: List[ParamSpec]) -> List[str]:
    """Consistency errors between declarations and the code's params usage.

    Declared-but-unreferenced params would render dead controls in the UI;
    referenced-but-undeclared names would never receive a value. Only applies
    when the code opts into params (declares the argument)."""
    errors: List[str] = []
    declared = {s.name for s in specs}
    referenced = extract_code_param_names(code)
    if not code_accepts_params(code):
        if declared:
            errors.append(
                "code does not accept a `params` argument but parameters are declared"
            )
        return errors
    for name in sorted(declared - referenced):
        errors.append(f"declared parameter '{name}' is never read by the code")
    for name in sorted(referenced - declared):
        errors.append(f"code reads params['{name}'] but no such parameter is declared")
    return errors


def verify_identity_binds_in_queries(
    captured_queries: List[str],
    values: Dict[str, Any],
    specs: List[ParamSpec],
) -> Optional[str]:
    """Post-execution check: every identity-locked param whose value is set
    must appear (as its rendered literal content) in at least one executed
    query. Prevents "the LLM forgot the WHERE clause" from silently serving
    unscoped rows to a viewer. Returns an error message or None."""
    identity_specs = [s for s in specs if s.source == "identity"]
    if not identity_specs or not captured_queries:
        return None
    haystack = "\n".join(q for q in captured_queries if isinstance(q, str))
    for spec in identity_specs:
        value = values.get(spec.name)
        if value in (None, "", []):
            continue
        needles = value if isinstance(value, list) else [value]
        found = any(str(n) in haystack for n in needles if n not in (None, ""))
        if not found:
            return (
                f"identity parameter '{spec.name}' was not applied in any executed query; "
                "refusing to serve unscoped results"
            )
    return None
