"""Storage gates for create_artifact / edit_artifact (pure, no I/O).

An artifact may declare ``content.storage`` (see ``app.schemas.app_storage``)
and its code reads/writes records through ``useCollection("<name>")``. The
tools are mechanical, so everything here is a deterministic check the planner
can act on:

- ``storage_declaration_errors``: the declaration parses (RD8 strictness) and
  a field added to an EXISTING collection is optional or has a default (old
  records do not have it).
- ``storage_reference_errors``: every ``useCollection`` reference is a direct
  call naming a declared collection with a string literal, so the gate is
  decidable. Comments and string literals are ignored; aliases, optional
  calls, bracket access and destructuring are rejected.
- ``storage_changes`` / ``describe_storage_changes``: declaration changes that
  can hide or expose stored records (spec 12 plus scope / create-rule changes,
  fields made required, and collections re-declared over orphaned rows), with
  the record impact.

The impure parts are at the end: ``destructive_storage_changes`` (reads the
effective declaration and, only when storage is involved and something may
change, record counts), ``non_interactive_storage_guard`` (fail closed for
edit paths that cannot ask) and ``confirm_storage_changes`` (asks the run's
user through the durable builtin confirmation and fails closed otherwise).
"""
import re
import time
from typing import Any, AsyncIterator, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ValidationError

from app.ai.runner.policies import TimeoutPolicy
from app.schemas.app_storage import CollectionSpec, FieldSpec, StorageDeclaration, parse_storage_declaration

USE_COLLECTION_RE = re.compile(r"\buseCollection\s*\(")
# Any reference to the identifier (not part of a longer identifier).
_IDENT_RE = re.compile(r"(?<![\w$])useCollection(?![\w$])")
# A string literal whose whole value is the identifier (bracket access).
_NAME_STRING_RE = re.compile(r"""(["'`])useCollection\1""")
# A single string literal (no escapes, no template interpolation) followed by
# the end of the argument.
_LITERAL_ARG_RE = re.compile(r"""\s*(?:"([^"\\\n]*)"|'([^'\\\n]*)'|`([^`\\$\n]*)`)\s*[,)]""")


def _mask_js(code: str) -> Optional[Tuple[str, List[int]]]:
    """Blank out comments and string/template literal text (same length, same
    newlines) and list the offsets of literals whose value is exactly
    ``useCollection``. Template ``${...}`` expressions stay code.

    A '/" quote without a closing quote on the same line is not a string (an
    apostrophe in JSX text). Returns None when a block comment or template
    literal never closes, so the caller scans the raw code instead of
    masking the rest of the file.
    """
    n = len(code)
    out = list(code)
    names: List[int] = []

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    stack: List[Tuple[int, int]] = []  # (brace depth, template start) suspended at ${
    depth = 0
    in_template = False
    tpl_start = seg = 0
    i = 0
    while i < n:
        c = code[i]
        if in_template:
            if c == "\\":
                i += 2
            elif c == "`":
                blank(seg, i + 1)
                if seg == tpl_start and code[tpl_start + 1:i] == "useCollection":
                    names.append(tpl_start)
                in_template = False
                i += 1
            elif code.startswith("${", i):
                blank(seg, i + 2)
                stack.append((depth, tpl_start))
                depth, in_template = 0, False
                i += 2
            else:
                i += 1
            continue
        if code.startswith("//", i):
            j = code.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif code.startswith("/*", i):
            j = code.find("*/", i + 2)
            if j < 0:
                return None
            blank(i, j + 2)
            i = j + 2
        elif c in "\"'":
            j = i + 1
            while j < n and code[j] not in (c, "\n"):
                j += 2 if code[j] == "\\" else 1
            if j >= n or code[j] != c:
                i += 1  # stray quote: not a string
                continue
            if code[i + 1:j] == "useCollection":
                names.append(i)
            blank(i, j + 1)
            i = j + 1
        elif c == "`":
            in_template, tpl_start, seg = True, i, i
            i += 1
        else:
            if c == "{":
                depth += 1
            elif c == "}":
                if depth == 0 and stack:
                    # Closes a ${...}: back into the suspended template.
                    depth, tpl_start = stack.pop()
                    in_template, seg = True, i
                elif depth:
                    depth -= 1
            i += 1
    if in_template or stack:
        return None
    return "".join(out), names


def _references(code: str) -> List[Tuple[Optional[str], bool, str]]:
    """(literal name or None, is_direct_call, snippet) per useCollection reference, in order."""
    code = code or ""
    lexed = _mask_js(code)
    if lexed is None:
        masked, named = code, [m.start() for m in _NAME_STRING_RE.finditer(code)]
    else:
        masked, named = lexed

    def snippet(pos: int) -> str:
        return code[pos:pos + 50].split("\n", 1)[0]

    found: List[Tuple[int, Optional[str], bool, str]] = [(pos, None, False, snippet(pos)) for pos in named]
    for match in _IDENT_RE.finditer(masked):
        k = match.end()
        while k < len(masked) and masked[k].isspace():
            k += 1
        if k < len(masked) and masked[k] == "(":
            arg = _LITERAL_ARG_RE.match(code, k + 1)
            name = next(g for g in arg.groups() if g is not None) if arg is not None else None
            found.append((match.start(), name, True, snippet(match.start())))
        else:
            found.append((match.start(), None, False, snippet(match.start())))
    found.sort(key=lambda item: item[0])
    return [(name, direct, snip) for _, name, direct, snip in found]


def referenced_collections(code: str) -> List[str]:
    """Collection names used via direct string-literal ``useCollection`` calls, first-seen order."""
    names: List[str] = []
    for name, direct, _ in _references(code):
        if direct and name is not None and name not in names:
            names.append(name)
    return names


def storage_reference_errors(code: str, declaration: Optional[StorageDeclaration]) -> List[str]:
    """Every useCollection reference must be a direct call naming a declared collection with a string literal."""
    errors: List[str] = []
    declared = list(declaration.collections) if declaration is not None else []
    reported: set = set()
    for name, direct, snippet in _references(code):
        if not direct:
            errors.append(
                "[storage] call useCollection directly with a string literal, e.g. useCollection(\"notes\"); "
                "aliases, optional calls (?.), bracket access and destructuring hide which collection is used. "
                f"Found `{snippet}`."
            )
            continue
        if name is None:
            errors.append(
                "[storage] useCollection must be called with a single string-literal collection "
                f"name, e.g. useCollection(\"notes\"); found `{snippet}`."
            )
            continue
        if name in declared or name in reported:
            continue
        reported.add(name)
        if declaration is None:
            errors.append(
                f"[storage] useCollection(\"{name}\") uses collection '{name}' but the artifact declares "
                f"no storage. Pass `storage` with collections.{name} (scope and fields), or remove the call."
            )
        else:
            errors.append(
                f"[storage] useCollection(\"{name}\") uses collection '{name}', which is not declared in "
                f"storage.collections (declared: {', '.join(declared) or 'none'}). Declare it or use a declared name."
            )
    return errors


def _format_validation_error(exc: ValidationError) -> List[str]:
    errors = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc") or ())
        msg = err.get("msg") or "invalid"
        errors.append(f"[storage] {loc}: {msg}" if loc else f"[storage] {msg}")
    return errors


def storage_declaration_errors(new_raw: Any, previous: Optional[StorageDeclaration]) -> List[str]:
    """Validate a supplied declaration against the schema and the previous one.

    ``new_raw`` None means nothing was supplied (no errors).
    """
    if new_raw is None:
        return []
    try:
        new = parse_storage_declaration(new_raw)
    except ValidationError as exc:
        return _format_validation_error(exc)
    errors: List[str] = []
    if previous is None or new is None:
        return errors
    for cname, spec in new.collections.items():
        old = previous.collections.get(cname)
        if old is None:
            continue
        for fname, field in spec.fields.items():
            if fname in old.fields:
                continue
            if field.required and not field.has_default:
                errors.append(
                    f"[storage] new field '{fname}' in existing collection '{cname}' is required without a "
                    "default; existing records do not have it. Make it optional or give it a default."
                )
    return errors


class StorageChange(BaseModel):
    kind: Literal[
        "collection_removed",
        "field_removed",
        "field_type_changed",
        "scope_changed",
        "create_changed",
        "field_made_required",
        "collection_readded",
    ]
    collection: str
    field: Optional[str] = None
    before: Optional[str] = None
    after: Optional[str] = None
    records: int
    users: int


def _forces_presence(field: FieldSpec) -> bool:
    return field.required and not field.has_default


def _rules(spec: CollectionSpec) -> str:
    """Who sees / adds, e.g. 'per_user' or 'shared/owner'."""
    return spec.scope if spec.scope != "shared" else f"shared/{spec.create}"


def _collection_changes(
    name: str,
    old: CollectionSpec,
    new: CollectionSpec,
    impact: Dict[str, int],
    field_history: Dict[str, str],
) -> List[StorageChange]:
    changes: List[StorageChange] = []
    if old.scope != new.scope:
        changes.append(StorageChange(kind="scope_changed", collection=name, before=old.scope, after=new.scope, **impact))
    elif old.scope == "shared" and old.create != new.create:
        changes.append(StorageChange(kind="create_changed", collection=name, before=old.create, after=new.create, **impact))
    for fname, old_field in old.fields.items():
        new_field = new.fields.get(fname)
        if new_field is None:
            changes.append(StorageChange(kind="field_removed", collection=name, field=fname, before=old_field.type, **impact))
            continue
        if old_field.type != new_field.type:
            changes.append(StorageChange(
                kind="field_type_changed", collection=name, field=fname,
                before=old_field.type, after=new_field.type, **impact,
            ))
        if _forces_presence(new_field) and not _forces_presence(old_field):
            changes.append(StorageChange(kind="field_made_required", collection=name, field=fname, **impact))
    for fname, new_field in new.fields.items():
        # A field removed earlier keeps its stored values; re-adding it with
        # another type reads them as that type.
        earlier = field_history.get(fname)
        if fname not in old.fields and earlier is not None and earlier != new_field.type:
            changes.append(StorageChange(
                kind="field_type_changed", collection=name, field=fname,
                before=earlier, after=new_field.type, **impact,
            ))
    return changes


def _impact(stats: Dict[str, Dict[str, int]], name: str) -> Dict[str, int]:
    entry = stats.get(name) or {}
    return {"records": int(entry.get("records") or 0), "users": int(entry.get("users") or 0)}


def storage_changes(
    previous: Optional[StorageDeclaration],
    new: Optional[StorageDeclaration],
    stats: Dict[str, Dict[str, int]],
    field_history: Optional[Dict[str, Dict[str, str]]] = None,
) -> List[StorageChange]:
    """Every declaration change that can hide or expose stored records.

    Reported regardless of record count (spec 12); ``records``/``users`` come
    from ``stats`` (0 when a collection has no live records). ``new`` None
    means no storage at all, i.e. every previous collection is removed.
    A collection declared in ``new`` but not in ``previous`` (or with no
    previous declaration) is a change only when ``stats`` shows live rows
    under its name: records kept from an earlier declaration would reappear
    under the new rules. ``field_history`` maps collection -> field -> the
    type an earlier version declared, for fields re-added to a collection.
    """
    old_collections = previous.collections if previous is not None else {}
    new_collections = new.collections if new is not None else {}
    history = field_history or {}
    changes: List[StorageChange] = []
    for name, old_spec in old_collections.items():
        impact = _impact(stats, name)
        new_spec = new_collections.get(name)
        if new_spec is None:
            changes.append(StorageChange(kind="collection_removed", collection=name, **impact))
        else:
            changes.extend(_collection_changes(name, old_spec, new_spec, impact, history.get(name) or {}))
    for name, new_spec in new_collections.items():
        if name in old_collections:
            continue
        impact = _impact(stats, name)
        if impact["records"] > 0:
            changes.append(StorageChange(
                kind="collection_readded", collection=name, before="orphaned records", after=_rules(new_spec), **impact,
            ))
    return changes


def _describe(change: StorageChange) -> str:
    c, f = change.collection, change.field
    if change.kind == "collection_removed":
        what = f"Remove collection '{c}'"
    elif change.kind == "collection_readded":
        what = (
            f"Declare collection '{c}' again as {change.after}: records it kept from an earlier declaration "
            "become visible under the new rules"
        )
    elif change.kind == "field_removed":
        what = f"Remove field '{f}' from '{c}' (stored values are kept but no longer shown)"
    elif change.kind == "field_type_changed":
        what = f"Change field '{f}' in '{c}' from {change.before} to {change.after}"
    elif change.kind == "scope_changed":
        what = f"Change who sees '{c}' from {change.before} to {change.after}"
    elif change.kind == "create_changed":
        what = f"Change who can add to '{c}' from {change.before} to {change.after}"
    else:
        what = f"Make field '{f}' in '{c}' required without a default (older records without it can no longer be updated)"
    return f"- {what}: affects {change.records} record(s) from {change.users} user(s)"


def describe_storage_changes(changes: List[StorageChange]) -> str:
    """One line per change with its record impact ('' when there are none)."""
    return "\n".join(_describe(c) for c in changes)


# ---------------------------------------------------------------------------
# Confirmation budget (pure)
# ---------------------------------------------------------------------------

# How long the user gets to answer. Asked after render validation, so only the
# persist follows; the wait is further capped by the tool runner's budget.
STORAGE_CONFIRM_TIMEOUT_S: float = 150.0
# Fallback when the runner's absolute deadline is not in runtime_ctx (a tool
# driven outside ToolRunner): the default hard timeout from a call's start.
TOOL_HARD_TIMEOUT_S: float = float(TimeoutPolicy().hard_timeout_s)
# Headroom kept after the wait for the persist + observation (and the poll
# granularity of the wait loop).
STORAGE_CONFIRM_SAFETY_MARGIN_S: float = 20.0
# Below this there is no realistic chance for the user to see and answer.
STORAGE_CONFIRM_MIN_WAIT_S: float = 30.0

ConfirmReason = Literal["declined", "timed_out", "non_interactive", "insufficient_time", "stopped"]


def confirmation_deadline(runtime_ctx: Dict[str, Any], started_monotonic: Optional[float]) -> Optional[float]:
    """Monotonic time the tool call is killed at: the runner's deadline (it
    covers every retry attempt), else ``started_monotonic`` plus the default
    hard timeout, else unknown (None)."""
    deadline = runtime_ctx.get("tool_deadline_monotonic")
    if deadline is not None:
        return float(deadline)
    if started_monotonic is None:
        return None
    return started_monotonic + TOOL_HARD_TIMEOUT_S


def confirmation_wait_budget(deadline: Optional[float], now: float) -> Optional[float]:
    """Seconds to wait for the answer, or None when too little budget is left."""
    timeout_s = STORAGE_CONFIRM_TIMEOUT_S
    if deadline is None:
        return timeout_s
    remaining = deadline - STORAGE_CONFIRM_SAFETY_MARGIN_S - now
    if remaining < min(STORAGE_CONFIRM_MIN_WAIT_S, timeout_s):
        return None
    return min(timeout_s, remaining)


# ---------------------------------------------------------------------------
# Impure: DB + SSE
# ---------------------------------------------------------------------------

def _completed_versions_query(artifact_id: str):
    from sqlalchemy import select
    from app.models.artifact import ArtifactVersion

    return (
        select(ArtifactVersion.content)
        .where(
            ArtifactVersion.artifact_id == str(artifact_id),
            ArtifactVersion.status == "completed",
            ArtifactVersion.deleted_at.is_(None),
        )
        .order_by(ArtifactVersion.version.desc())
    )


async def effective_declaration_strict(db, artifact_id: str) -> Tuple[Optional[StorageDeclaration], bool]:
    """(effective declaration, invalid). Same version as
    ``app_data_service.effective_declaration``, but a declaration that does not
    parse is reported (invalid=True) instead of being treated as none, so a
    rebuild can fail closed rather than silently carry nothing."""
    content = (await db.execute(_completed_versions_query(artifact_id).limit(1))).scalar_one_or_none()
    raw = content.get("storage") if isinstance(content, dict) else None
    try:
        return parse_storage_declaration(raw), False
    except ValidationError:
        return None, True


async def _declared_field_types(db, artifact_id: str) -> Dict[str, Dict[str, str]]:
    """collection -> field -> type most recently declared by a completed version."""
    history: Dict[str, Dict[str, str]] = {}
    for content in (await db.execute(_completed_versions_query(artifact_id))).scalars().all():
        raw = content.get("storage") if isinstance(content, dict) else None
        try:
            declaration = parse_storage_declaration(raw)
        except ValidationError:
            continue
        for cname, spec in (declaration.collections if declaration is not None else {}).items():
            fields = history.setdefault(cname, {})
            for fname, field in spec.fields.items():
                fields.setdefault(fname, field.type)
    return history


async def destructive_storage_changes(db, artifact_id: str, new_raw: Any) -> List[StorageChange]:
    """Changes from the artifact's effective declaration to ``new_raw``.

    Raises ValidationError when ``new_raw`` does not parse. Record counts
    (app_records) are read only when storage is involved on either side and
    a change or a newly declared collection needs them (RD12); earlier
    versions' declarations only when a field is added to a kept collection.
    """
    from app.services.app_data_service import app_data_service

    previous = await app_data_service.effective_declaration(db, str(artifact_id))
    new = parse_storage_declaration(new_raw)
    if previous is None and new is None:
        return []
    old_collections = previous.collections if previous is not None else {}
    new_collections = new.collections if new is not None else {}
    added = [name for name in new_collections if name not in old_collections]
    field_added = any(
        fname not in old_collections[cname].fields
        for cname, spec in new_collections.items() if cname in old_collections
        for fname in spec.fields
    )
    history = await _declared_field_types(db, str(artifact_id)) if field_added else {}
    if not added and not storage_changes(previous, new, {}, history):
        return []
    stats = await app_data_service.collection_stats(db, str(artifact_id))
    return storage_changes(previous, new, stats, history)


def _change_names(changes: List[StorageChange]) -> str:
    return ", ".join(
        f"{c.kind} '{c.collection}" + (f".{c.field}'" if c.field else "'") for c in changes
    )


async def non_interactive_storage_guard(db, artifact, new_storage: Any) -> Optional[str]:
    """Fail-closed check for edit paths that cannot ask the user (MCP, legacy
    edit). They carry the edited version's declaration as-is, so editing an
    older or failed version could change the effective declaration without
    approval. Returns an error message when anything would change, else None.
    """
    try:
        changes = await destructive_storage_changes(db, str(artifact.artifact_id), new_storage)
    except ValidationError:
        return (
            "This version's storage declaration is invalid, so editing it would drop the artifact's record "
            "storage. Nothing was applied. Edit the latest version through an interactive run instead."
        )
    if not changes:
        return None
    return (
        f"Editing this version would change the artifact's storage declaration ({_change_names(changes)}), "
        "which can hide or expose stored records, and this path cannot ask the user to approve it. Nothing was "
        "applied. Edit the latest version through an interactive run instead.\n"
        + describe_storage_changes(changes)
    )


async def confirm_storage_changes(
    runtime_ctx: Dict[str, Any],
    *,
    tool_name: str,
    changes: List[StorageChange],
    started_monotonic: Optional[float] = None,
) -> AsyncIterator[Any]:
    """Ask the run's user to approve ``changes``; yields tool events, then a
    final ``{"approved": bool, "reason": Optional[ConfirmReason]}``.

    Fails closed without asking when nobody can answer (non-interactive run),
    when the run was stopped, or when too little of the runner's hard budget
    is left for a meaningful wait. Pressing Stop during the wait -> "stopped".
    """
    from app.ai.tools.confirmation import KIND_BUILTIN_TOOL, stream_user_confirmation
    from app.services.tool_policy_service import ToolPolicyService

    sigkill = runtime_ctx.get("sigkill_event")
    if not ToolPolicyService.is_interactive_run(runtime_ctx):
        yield {"approved": False, "reason": "non_interactive"}
        return
    if sigkill is not None and sigkill.is_set():
        yield {"approved": False, "reason": "stopped"}
        return
    timeout_s = confirmation_wait_budget(confirmation_deadline(runtime_ctx, started_monotonic), time.monotonic())
    if timeout_s is None:
        yield {"approved": False, "reason": "insufficient_time"}
        return

    result: Dict[str, Any] = {}
    async for event in stream_user_confirmation(
        runtime_ctx,
        kind=KIND_BUILTIN_TOOL,
        tool_name=tool_name,
        payload={
            "storage_changes": [c.model_dump(mode="json") for c in changes],
            "summary": describe_storage_changes(changes),
        },
        result=result,
        timeout_s=timeout_s,
    ):
        yield event
    if sigkill is not None and sigkill.is_set():
        yield {"approved": False, "reason": "stopped"}
    elif result.get("approved"):
        yield {"approved": True, "reason": None}
    elif result.get("timed_out"):
        yield {"approved": False, "reason": "timed_out"}
    else:
        yield {"approved": False, "reason": "declined"}


def storage_confirmation_failure(reason: Optional[str], changes: List[StorageChange]) -> str:
    """Observation text for a change that was not approved."""
    why = {
        "declined": "The user declined",
        "timed_out": "Nobody answered the approval request in time for",
        "non_interactive": "This run cannot ask the user (not an interactive session), so it cannot apply",
        "insufficient_time": "Too little of this tool call's time budget was left to ask the user about",
        "stopped": "The run was stopped by the user while waiting for approval of",
    }.get(reason or "", "The user did not approve")
    advice = {
        "declined": "Keep the existing collections and fields unless the user explicitly asks for this change.",
        "timed_out": "Ask the user whether they want this change before trying again.",
        "non_interactive": "Keep the existing declaration; such changes need an interactive session.",
        "insufficient_time": (
            "Call edit_artifact again with only the `storage` change so the approval can be requested."
        ),
        "stopped": "Do not retry unless the user asks again.",
    }.get(reason or "", "")
    return (
        f"{why} the storage change(s) below, which can hide or expose stored records; nothing was applied. "
        f"{advice}\n{describe_storage_changes(changes)}"
    ).strip()
