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
  change who can read or change stored records, with the record impact.
  Access is compared by evaluating the SAME rule functions that enforce it
  (``app_data_rules``) on the old and the new declaration for every principal
  class; any difference is a change. Data-level changes (removed or re-added
  collections and fields, type changes, fields made required) are detected
  separately because stored values survive removal (spec 12, revised).

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
from app.services.app_data_rules import can_create, can_modify, can_read, visible_author_filter

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


_STORAGE_IN_TEXT_RE = re.compile(r'"storage"|\bstorage\s*=')
_UNDECLARED_MARKERS = ("declares no storage", "not declared in storage.collections")
MISPLACED_STORAGE_HINT = (
    " It looks like the storage declaration was placed inside `prompt`; pass it as the separate top-level "
    "`storage` argument."
)


def with_misplaced_storage_hint(errors: List[str], prompt: Optional[str]) -> List[str]:
    """For a call without `storage`: add a hint to undeclared-collection
    errors when ``prompt`` looks like it carries the declaration."""
    if not errors or not prompt or not _STORAGE_IN_TEXT_RE.search(prompt):
        return errors
    return [e + MISPLACED_STORAGE_HINT if any(m in e for m in _UNDECLARED_MARKERS) else e for e in errors]


_WRITE_CALL_RE = re.compile(r"(?<![\w$])(?:add|update|remove)\s*\(")
_CATCH_RE = re.compile(r"(?<![\w$])catch(?![\w$])")


def write_handling_note(code: str) -> str:
    """Non-blocking note when code writes to a collection but never catches.

    Comments and string literals are ignored. Returns '' when there is
    nothing to say.
    """
    code = code or ""
    lexed = _mask_js(code)
    masked = lexed[0] if lexed is not None else code
    if not USE_COLLECTION_RE.search(masked) or not _WRITE_CALL_RE.search(masked) or _CATCH_RE.search(masked):
        return ""
    return (
        " NOTE: the code writes to a collection (add/update/remove) but has no `catch`: write rejections "
        "(forbidden, validation, conflict, ...) would be unhandled. Wrap every write in try/await/catch or add "
        "`.catch(() => {})`, render the collection's `error`, and fix it with edit_artifact."
    )


_CREATE_WORDS = {"members": "members", "owner": "owner only"}
_MODIFY_WORDS = {"author": "each author their own (owner: any)", "owner": "owner only"}
_PUBLIC_WORDS = {True: "owner's records visible", False: "not visible"}


def describe_storage_rules(declaration: Optional[StorageDeclaration]) -> str:
    """The effective sharing rules in plain words ('' without collections)."""
    if declaration is None or not declaration.collections:
        return ""
    parts = []
    for name, spec in declaration.collections.items():
        if spec.scope == "per_user":
            parts.append(f"{name} — private per viewer.")
        else:
            parts.append(
                f"{name} — shared; add: {_CREATE_WORDS[spec.create]}; edit/delete: {_MODIFY_WORDS[spec.modify]}; "
                f"public link: {_PUBLIC_WORDS[spec.public_read]}."
            )
    return "Storage: " + " ".join(parts)


def storage_success_note(declaration: Optional[StorageDeclaration], code: str) -> str:
    """Summary suffix after a successful create/edit: the effective rules for
    the planner to check against the request, plus the write-handling note."""
    rules = describe_storage_rules(declaration)
    note = write_handling_note(code)
    if rules:
        rules = (
            f" {rules} Check these rules against the user's words; if they differ, fix with edit_artifact (storage)."
        )
    return rules + note


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
        "collection_readded",
        "field_removed",
        "field_readded",
        "field_type_changed",
        "field_made_required",
        "access_changed",
    ]
    collection: str
    field: Optional[str] = None
    # access_changed only: who ("owner", "member", "public" = outsiders and
    # anonymous visitors alike, or "outsider"/"anonymous" when they differ)
    # and what ("read": none|own|owner|all; the others: yes|no).
    principal: Optional[str] = None
    capability: Optional[str] = None
    before: Optional[str] = None
    after: Optional[str] = None
    records: int
    users: int


# ---------------------------------------------------------------------------
# Effective access, evaluated with the enforcing rule functions
# ---------------------------------------------------------------------------

_PRINCIPALS = ("owner", "member", "outsider", "anonymous")
_CAPABILITIES = ("read", "create", "modify_own", "modify_others")
# Stand-in ids: only equality between them matters to the rules.
_OWNER_ID = "report-owner"
_SAMPLE_ID = {"owner": _OWNER_ID, "member": "a-member", "outsider": "an-outsider", "anonymous": None}
_OTHER_ID = "someone-else"


def _read_level(spec: CollectionSpec, principal: str) -> str:
    """Whose records ``principal`` reads: none, own, owner (the report owner's) or all."""
    if not can_read(spec, principal):
        return "none"
    user_id = _SAMPLE_ID[principal]
    try:
        author = visible_author_filter(spec, principal, user_id=user_id, owner_id=_OWNER_ID)
    except ValueError:
        return "none"
    if author is None:
        return "all"
    return "own" if author == user_id else "owner"


def _access(spec: CollectionSpec, principal: str) -> Dict[str, str]:
    user_id = _SAMPLE_ID[principal]

    def yes(allowed: bool) -> str:
        return "yes" if allowed else "no"

    return {
        "read": _read_level(spec, principal),
        "create": yes(can_create(spec, principal)),
        "modify_own": yes(can_modify(spec, principal, user_id=user_id, record_user_id=user_id or _OTHER_ID)),
        "modify_others": yes(can_modify(spec, principal, user_id=user_id, record_user_id=_OTHER_ID)),
    }


def _access_changes(name: str, old: CollectionSpec, new: CollectionSpec, stats: Dict[str, int]) -> List[StorageChange]:
    """Every (principal, capability) whose answer differs between ``old`` and
    ``new``. Outsiders and anonymous visitors (both reach an artifact through
    its public link) are reported together as "public" when they change alike."""
    before = {p: _access(old, p) for p in _PRINCIPALS}
    after = {p: _access(new, p) for p in _PRINCIPALS}
    impact = {"records": stats["records"], "users": stats["users"]}
    owner_rows = stats["owner_records"]
    changes: List[StorageChange] = []
    for group in (("owner",), ("member",), ("outsider", "anonymous")):
        for capability in _CAPABILITIES:
            diffs = [(p, before[p][capability], after[p][capability]) for p in group
                     if before[p][capability] != after[p][capability]]
            if len(group) == 2 and len(diffs) == 2 and diffs[0][1:] == diffs[1][1:]:
                diffs = [("public", *diffs[0][1:])]
            for principal, was, becomes in diffs:
                rows = impact
                if capability == "read" and "owner" in (was, becomes) and principal != "owner":
                    # Public-link readers see the owner's records only.
                    rows = {"records": owner_rows, "users": 1 if owner_rows else 0}
                changes.append(StorageChange(
                    kind="access_changed", collection=name, principal=principal, capability=capability,
                    before=was, after=becomes, **rows,
                ))
    return changes


def _forces_presence(field: FieldSpec) -> bool:
    return field.required and not field.has_default


def _rules(spec: CollectionSpec) -> str:
    """Who sees / adds, e.g. 'per_user', 'shared/members' or 'shared/owner/public'."""
    if spec.scope != "shared":
        return spec.scope
    return f"shared/{spec.create}" + ("/public" if spec.public_read else "")


def _collection_changes(
    name: str,
    old: CollectionSpec,
    new: CollectionSpec,
    stats: Dict[str, int],
    field_history: Dict[str, str],
) -> List[StorageChange]:
    impact = {"records": stats["records"], "users": stats["users"]}
    changes: List[StorageChange] = _access_changes(name, old, new, stats)
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
        # A field removed earlier keeps its stored values; re-adding it (any
        # type) returns them again.
        earlier = field_history.get(fname)
        if fname not in old.fields and earlier is not None:
            changes.append(StorageChange(
                kind="field_readded", collection=name, field=fname,
                before=earlier, after=new_field.type, **impact,
            ))
    return changes


def _impact(stats: Dict[str, Dict[str, int]], name: str) -> Dict[str, int]:
    entry = stats.get(name) or {}
    return {key: int(entry.get(key) or 0) for key in ("records", "users", "owner_records")}


def storage_changes(
    previous: Optional[StorageDeclaration],
    new: Optional[StorageDeclaration],
    stats: Dict[str, Dict[str, int]],
    field_history: Optional[Dict[str, Dict[str, str]]] = None,
) -> List[StorageChange]:
    """Every declaration change that changes who can read or change stored records.

    Reported regardless of record count (spec 12). ``stats`` maps collection
    -> ``records``/``users`` (live rows and distinct authors) and
    ``owner_records`` (live rows written by the report owner); missing
    entries count 0. For collections in both declarations, the access of
    every principal class is compared (``access_changed``); fields removed,
    re-added (``field_history``: collection -> field -> the type an earlier
    version declared), retyped or made required are data-level changes.
    ``new`` None means no storage at all, i.e. every previous collection is
    removed. A collection declared in ``new`` but not in ``previous`` is a
    change only when ``stats`` shows live rows under its name: records kept
    from an earlier declaration would reappear under the new rules.
    """
    old_collections = previous.collections if previous is not None else {}
    new_collections = new.collections if new is not None else {}
    history = field_history or {}
    changes: List[StorageChange] = []
    for name, old_spec in old_collections.items():
        entry = _impact(stats, name)
        new_spec = new_collections.get(name)
        if new_spec is None:
            changes.append(StorageChange(
                kind="collection_removed", collection=name, records=entry["records"], users=entry["users"],
            ))
        else:
            changes.extend(_collection_changes(name, old_spec, new_spec, entry, history.get(name) or {}))
    for name, new_spec in new_collections.items():
        if name in old_collections:
            continue
        entry = _impact(stats, name)
        if entry["records"] > 0:
            changes.append(StorageChange(
                kind="collection_readded", collection=name, before="orphaned records", after=_rules(new_spec),
                records=entry["records"], users=entry["users"],
            ))
    return changes


# Outcome sentences per (principal, capability, after) of an access change.
_ACCESS_OUTCOMES = {
    ("owner", "read", "all"): "You (the owner) will see every record in '{c}', including other users' records",
    ("owner", "read", "own"): "You (the owner) will see only your own records in '{c}'; other users' records will be hidden",
    ("owner", "modify_others", "yes"): "You (the owner) will be able to edit or delete other users' records in '{c}'",
    ("owner", "modify_others", "no"): "You (the owner) will no longer be able to edit or delete other users' records in '{c}'",
    ("member", "read", "all"): "Members will see every record in '{c}', including other users' records",
    ("member", "read", "own"): "Members will see only their own records in '{c}'; other users' records will be hidden",
    ("member", "create", "yes"): "Members will be able to add records to '{c}'",
    ("member", "create", "no"): "Members will no longer be able to add records to '{c}'",
    ("member", "modify_own", "yes"): "Members will be able to edit or delete their own existing records in '{c}'",
    ("member", "modify_own", "no"): "Members will no longer be able to edit or delete their own records in '{c}'",
    ("member", "modify_others", "yes"): "Members will be able to edit or delete other users' records in '{c}'",
    ("member", "modify_others", "no"): "Members will no longer be able to edit or delete other users' records in '{c}'",
    ("public", "read", "owner"): (
        "Anyone with the public link will be able to read {n} existing record(s) written by the owner in '{c}' "
        "(when the artifact is public)"
    ),
    ("public", "read", "none"): "Visitors with the public link will no longer be able to read '{c}'",
}


def _describe(change: StorageChange) -> str:
    c, f = change.collection, change.field
    impact = f": affects {change.records} record(s) from {change.users} user(s)"
    if change.kind == "access_changed":
        outcome = _ACCESS_OUTCOMES.get((change.principal, change.capability, change.after))
        if outcome is None:
            what = (f"Change what {change.principal} can do in '{c}': "
                    f"{change.capability} from {change.before} to {change.after}")
        elif "{n}" in outcome:
            return "- " + outcome.format(c=c, n=change.records)
        else:
            what = outcome.format(c=c)
    elif change.kind == "collection_removed":
        what = f"Remove collection '{c}'"
    elif change.kind == "collection_readded":
        what = (
            f"Declare collection '{c}' again as {change.after}: records it kept from an earlier declaration "
            "become visible under the new rules"
        )
    elif change.kind == "field_removed":
        what = f"Remove field '{f}' from '{c}' (stored values are kept but no longer shown)"
    elif change.kind == "field_readded":
        what = f"Re-add field '{f}' to '{c}' as {change.after}: values stored before it was removed become visible again"
        if change.before != change.after:
            what += f" (they were stored as {change.before})"
    elif change.kind == "field_type_changed":
        what = f"Change field '{f}' in '{c}' from {change.before} to {change.after}"
    else:
        what = f"Make field '{f}' in '{c}' required without a default (older records without it can no longer be updated)"
    return f"- {what}{impact}"


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
    (app_records, including rows written by the report owner) are read only
    when storage is involved on either side and a change or a newly declared
    collection needs them (RD12); earlier versions' declarations only when a
    field is added to a kept collection.
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
    for name, owner_records in (await app_data_service.owner_record_counts(db, str(artifact_id))).items():
        stats.setdefault(name, {"records": 0, "users": 0})["owner_records"] = owner_records
    return storage_changes(previous, new, stats, history)


def _change_names(changes: List[StorageChange]) -> str:
    def name(c: StorageChange) -> str:
        text = f"{c.kind} '{c.collection}" + (f".{c.field}'" if c.field else "'")
        if c.kind == "access_changed":
            text += f" ({c.principal} {c.capability}: {c.before} -> {c.after})"
        return text

    return ", ".join(name(c) for c in changes)


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
        "which changes who can read or change stored records, and this path cannot ask the user to approve it. "
        "Nothing was applied. Edit the latest version through an interactive run instead.\n"
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
        f"{why} the storage change(s) below, which change who can read or change stored records; "
        "nothing was applied. "
        f"{advice}\n{describe_storage_changes(changes)}"
    ).strip()
