"""Central redaction for agent-generated code (SQL / Python).

Generated code is a *field on the object*, not something behind a route: it
rides along in step payloads, tool-execution results and the live agent
stream. Hiding the editor in the UI therefore proves nothing — the code is
already in JSON the browser received. Everything that serializes code for a
human calls into this module instead.

Three surfaces carry it, and all three go through here:

1. Step payloads     — ``StepSchema.code`` / ``PublicStepSchema.code``
2. Tool results      — ``result_json`` from ``create_data`` / ``inspect_data``
3. The agent stream  — ``stage=generated_code`` progress events

Redaction sets code to ``None``, never ``""``. An empty string is
indistinguishable from a legitimately empty step, which would make the
difference between "withheld" and "there was none" untestable.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

VIEW_CODE = "view_code"
RUN_CUSTOM_CODE = "run_custom_code"

# Request-scoped visibility flag, mirroring the PII display redactor in
# ``app/ai/llm/pii/display.py``. The sync serializers (``serialize_block_v2_sync``,
# the ``StepSchema`` field serializer) cannot take an extra argument, and
# threading one through every call site is exactly how a surface gets forgotten.
#
# The default is DENY. That choice is deliberate: if some surface never sets the
# flag, an *authorized* user sees a missing code block — loud, and caught by the
# e2e suite — instead of an *unauthorized* user silently seeing code. The failure
# mode points the safe way.
_code_visible: ContextVar[Optional[bool]] = ContextVar("_code_visible", default=None)


@contextmanager
def code_visibility(allowed: bool):
    """Expose a resolved code-visibility decision to the sync serializers."""
    token = _code_visible.set(bool(allowed))
    try:
        yield allowed
    finally:
        _code_visible.reset(token)


def set_code_visibility(allowed: bool) -> None:
    """Set the request-scoped decision without unwinding (route decorators).

    FastAPI runs each request in its own context, so a value set here does not
    bleed into another request.
    """
    _code_visible.set(bool(allowed))


def code_visible_now() -> bool:
    """Current request's decision; deny when nothing set it."""
    return bool(_code_visible.get())

# Keys inside a tool ``result_json`` that carry generated source or the SQL it
# produced. This is a deliberate ALLOWLIST, not a sweep for keys named "code":
# tool error payloads use ``{"code": "INVALID_INPUT"}`` as an error *identifier*,
# and artifact tools keep the rendered component's source under ``code`` — the
# artifact iframe needs it to render at all. Blanket-stripping every "code" key
# would corrupt both.
# Tools whose ``result_json`` carries *analysis* code — the SQL/Python the agent
# writes to answer a question. Scoping by tool is required, not cosmetic: the
# artifact tools also key their payload on "code", but there the value IS the
# rendered component's source, which the artifact iframe needs to display
# anything at all. Redacting those would break rendering for the very users this
# feature is meant to keep working.
CODE_PRODUCING_TOOLS = frozenset({
    "create_data",
    "inspect_data",
    "write_csv",
    "create_widget",
})

_RESULT_CODE_KEYS = (
    "code",              # the generated query/script
    "errors",            # [[code, error], ...] — the failed attempts' source
    "executed_queries",  # the SQL actually sent to the source ("Queries" tab)
)


def can_view_code(resolved) -> bool:
    """True when the resolved permissions allow seeing generated code."""
    if resolved is None:
        return False
    return resolved.has_org_permission(VIEW_CODE)


def can_run_custom_code(resolved) -> bool:
    """True when the caller may submit and execute their own code."""
    if resolved is None:
        return False
    return resolved.has_org_permission(RUN_CUSTOM_CODE)


def redact_step_dict(step: dict, allowed: bool) -> dict:
    """Strip generated code from a serialized step mapping (in place)."""
    if allowed or not isinstance(step, dict):
        return step
    if "code" in step:
        step["code"] = None
    return step


def redact_step_schema(step, allowed: bool):
    """Strip generated code from a StepSchema/PublicStepSchema instance.

    Mutates and returns the model so callers can use it inline. Pydantic models
    are mutable here, and the alternative (``model_copy``) would silently drop
    the enrichment that ``_enrich_step_schema`` attaches.
    """
    if allowed or step is None:
        return step
    if hasattr(step, "code"):
        try:
            step.code = None
        except Exception:
            # A frozen/validating model must never break serialization; fall
            # back to leaving the object alone only if we truly cannot set it.
            pass
    return step


def _redact_query_timings(entries):
    """Null the statement text in a list of per-query timing records.

    The measurements stay: "how long did it take / how many rows" is not code,
    and a viewer who may not read the query is still entitled to know it ran.
    """
    if not isinstance(entries, list):
        return entries
    return [
        {**e, "sql": None} if isinstance(e, dict) and "sql" in e else e
        for e in entries
    ]


def redact_tool_result(result, allowed: bool, tool_name: str | None = None):
    """Strip generated code from a tool ``result_json`` payload.

    Walks the nested ``results`` list the same way ``project_tool_result_for_ui``
    does, so a batched tool call cannot smuggle code through a sub-result.

    ``tool_name`` scopes the redaction to the analysis tools; anything else is
    returned untouched (see CODE_PRODUCING_TOOLS).
    """
    if allowed or not isinstance(result, dict):
        return result
    if tool_name is not None and tool_name not in CODE_PRODUCING_TOOLS:
        return result

    redacted = dict(result)
    for key in _RESULT_CODE_KEYS:
        if key in redacted:
            redacted[key] = None

    # Per-query timing records embed the executed statement verbatim, so the
    # query was readable one key over from the redacted `code`. Structured
    # rather than nulled, so the timings themselves survive.
    if "query_timings" in redacted:
        redacted["query_timings"] = _redact_query_timings(redacted["query_timings"])

    # `error.failed_sql` carries the statement that failed, verbatim.
    err = redacted.get("error")
    if isinstance(err, dict) and "failed_sql" in err:
        err = dict(err)
        err["failed_sql"] = None
        redacted["error"] = err

    nested = redacted.get("results")
    if isinstance(nested, list):
        redacted["results"] = [
            redact_tool_result(item, allowed, tool_name) if isinstance(item, dict) else item
            for item in nested
        ]

    return redacted


# Progress stages whose payload carries generated source. Matching on the stage
# — not merely on the presence of a "code" key — is what keeps tool ERROR
# payloads intact: those use {"error": ..., "code": "FORBIDDEN"} where `code` is
# an error identifier, and nulling it would break error rendering.
_CODE_BEARING_STAGES = frozenset({"generating_code", "generated_code", "executing_code"})


def redact_tool_arguments(arguments, allowed: bool, tool_name: str | None = None):
    """Strip code from a tool's ``arguments_json``.

    The tool CALL carries the code for tools like ``write_csv`` (the UI reads
    ``arguments_json.code`` as a fallback), so redacting only ``result_json``
    would leave the same source readable one key over.
    """
    if allowed or not isinstance(arguments, dict):
        return arguments
    if tool_name is not None and tool_name not in CODE_PRODUCING_TOOLS:
        return arguments
    if "code" not in arguments:
        return arguments
    redacted = dict(arguments)
    redacted["code"] = None
    return redacted


def redact_tool_timings(timings, allowed: bool, tool_name: str | None = None):
    """Strip statement text from per-query timing telemetry.

    ``sub_timings_json`` records one entry per executed query as
    ``{index, query_ms, rows, result_bytes, sql}``. The ``sql`` there is the
    full statement — so a caller denied ``code`` could still read the query
    off the timing panel. The measurements themselves are not code and stay,
    because "how long did this take" is useful to a viewer who may not see how
    it was written.
    """
    if allowed or not isinstance(timings, dict):
        return timings
    if tool_name is not None and tool_name not in CODE_PRODUCING_TOOLS:
        return timings

    queries = timings.get("queries")
    if not isinstance(queries, list):
        return timings

    redacted = dict(timings)
    redacted["queries"] = _redact_query_timings(queries)
    return redacted


def redact_progress_payload(payload, allowed: bool):
    """Strip generated code from a streamed tool-progress event payload.

    The stream is the surface a UI-only approach misses: the client caches
    ``stage=generated_code`` payloads into ``progress_code`` and renders them
    live, before any REST serializer is involved.
    """
    if allowed or not isinstance(payload, dict):
        return payload
    if payload.get("stage") not in _CODE_BEARING_STAGES:
        return payload
    redacted = dict(payload)
    for key in ("code", "errors"):
        if key in redacted:
            redacted[key] = None
    return redacted
