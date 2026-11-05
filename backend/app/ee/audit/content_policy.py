# Audit content policy (docs/design/audit-event-contract.md, "Content policy")
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details
#
# Applied once, before anything leaves the producer: database writes, the
# disk spill, exports and diagnostic logging of audit failures all receive
# details that already went through ``sanitize_details``. Request context is
# an allowlist (ip, user agent, request id, session id): headers and bodies
# are never copied.

import json
import re
from typing import Any, Dict, List, Optional, Tuple

REDACTED = "[REDACTED]"

# Keys whose values are reusable authentication material or raw secrets.
_SECRET_KEY_RE = re.compile(
    r"(^|_|-)(password|passwd|pwd|secret|token|api[_-]?key|apikey|authorization|cookie|set-cookie|"
    r"hmac|private[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"credential|credentials|session[_-]?token|bearer|assertion|saml[_-]?response)($|_|-)",
    re.IGNORECASE,
)
# Values that are secrets whatever their key: JWTs, bearer strings, Fernet
# tokens, AWS access keys, PEM blocks.
_SECRET_VALUE_RES = (
    re.compile(r"^eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}$"),
    re.compile(r"^(Bearer|Splunk|Basic)\s+\S{8,}$", re.IGNORECASE),
    re.compile(r"^gAAAAA[A-Za-z0-9_-]{20,}={0,2}$"),
    re.compile(r"^(AKIA|ASIA)[A-Z0-9]{16}$"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"^sk-[A-Za-z0-9_-]{16,}$"),
)

# Default cap for a record's details payload. Beyond it the payload is
# replaced by a marker that says what was dropped (never silently cut).
DEFAULT_MAX_DETAILS_BYTES = 64 * 1024
# Default cap for the executed-query payload inside details. Every query is
# kept; the longest are shortened from the tail with an explicit marker.
DEFAULT_MAX_QUERY_PAYLOAD_BYTES = 32 * 1024


def _max_details_bytes() -> int:
    try:
        from app.settings.config import settings
        return int(settings.bow_config.audit.max_details_bytes)
    except Exception:
        return DEFAULT_MAX_DETAILS_BYTES


def _max_query_payload_bytes() -> int:
    try:
        from app.settings.config import settings
        return int(settings.bow_config.audit.max_query_payload_bytes)
    except Exception:
        return DEFAULT_MAX_QUERY_PAYLOAD_BYTES


def is_secret_key(key: Any) -> bool:
    return isinstance(key, str) and bool(_SECRET_KEY_RE.search(key))


def looks_like_secret(value: Any) -> bool:
    if not isinstance(value, str) or len(value) < 8:
        return False
    v = value.strip()
    return any(r.search(v) for r in _SECRET_VALUE_RES)


def _sanitize(value: Any, depth: int = 0) -> Any:
    if depth > 12:
        return REDACTED
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if is_secret_key(k):
                out[k] = REDACTED if v not in (None, "") else v
            else:
                out[k] = _sanitize(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize(v, depth + 1) for v in value]
    if isinstance(value, str):
        return REDACTED if looks_like_secret(value) else value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def payload_bytes(value: Any) -> int:
    return len(json.dumps(value, default=str, separators=(",", ":")).encode("utf-8"))


def sanitize_details(details: Optional[dict], *, max_bytes: Optional[int] = None) -> Optional[dict]:
    """Redact secrets and bound the size of an event's details.

    Returns a new dict (the caller's object is never mutated). An oversized
    payload is replaced by a marker that keeps the keys and the dropped size,
    so a consumer can tell that evidence was truncated and by how much.
    """
    if details is None:
        return None
    if not isinstance(details, dict):
        details = {"value": details}
    clean = _sanitize(details)
    cap = max_bytes if max_bytes is not None else _max_details_bytes()
    size = payload_bytes(clean)
    if size <= cap:
        return clean
    kept = {k: v for k, v in clean.items() if isinstance(v, (str, int, float, bool)) and payload_bytes(v) <= 512}
    kept["_truncated"] = True
    kept["_original_bytes"] = size
    kept["_dropped_keys"] = sorted(k for k in clean.keys() if k not in kept)
    return kept


def bounded_queries(queries: Optional[List[Any]], *, max_bytes: Optional[int] = None) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    """Every executed query, bounded by total size instead of count.

    Returns ``(queries, truncation)``. ``truncation`` is None when nothing
    was cut; otherwise it names which queries were shortened and their
    original lengths. Queries are never dropped: when the budget is smaller
    than the number of queries, each keeps a short head and the marker says
    so.
    """
    items = [str(q) if q is not None else "" for q in (queries or [])]
    cap = max_bytes if max_bytes is not None else _max_query_payload_bytes()
    total = sum(len(q.encode("utf-8")) for q in items)
    if total <= cap:
        return items, None
    n = len(items)
    per = max(64, cap // max(n, 1))
    out, shortened = [], []
    for i, q in enumerate(items):
        if len(q.encode("utf-8")) > per:
            cut = q.encode("utf-8")[:per].decode("utf-8", errors="ignore")
            out.append(cut + "…")
            shortened.append({"index": i, "original_length": len(q), "kept_length": len(cut)})
        else:
            out.append(q)
    return out, {"truncated": True, "query_count": n, "original_bytes": total, "limit_bytes": cap, "shortened": shortened}


def with_queries(details: Optional[dict], queries: Optional[List[Any]], *, key: str = "executed_queries") -> dict:
    """Attach executed queries to details under the content policy."""
    out = dict(details or {})
    qs, trunc = bounded_queries(queries)
    out[key] = qs
    if trunc:
        out[f"{key}_truncation"] = trunc
    return out
