"""Validate and persist list records (agent submissions and human edits).

Invariants this module owns:
- A submission is atomic: every record is validated first; if any record is
  invalid nothing is written and every error comes back path-qualified
  (``records.0.fields.annual_value.value: ...``) so the agent can fix and retry.
- Matching an existing row: ``row_id`` first, then the list's key field,
  otherwise insert.
- Fields a human edited are *locked*: agent updates skip them and report the
  skip, so human corrections survive scheduled reruns.
- Every change writes one ``AgentListRowRevision``.
"""
import copy
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.agent_list import AgentList, AgentListRow, AgentListRowRevision
from app.services.agent_lists.compiler import compile_list_schema
from app.services.agent_lists.verify import SourceText, verify_quote

logger = logging.getLogger(__name__)

MAX_RECORDS_PER_CALL = 200


class ListValidationError(Exception):
    def __init__(self, errors: List[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class RowConflictError(Exception):
    """Stale row_version or key collision on a human edit."""


# ── value coercion / checks ────────────────────────────────────────────────

def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def check_value(field: Dict[str, Any], value: Any) -> Optional[str]:
    """Return an error message when ``value`` does not fit ``field`` (None is fine)."""
    if value is None:
        return None
    t = field.get("type") or "string"
    if t == "string":
        return None if isinstance(value, str) else "expected text"
    if t == "number":
        return None if (isinstance(value, (int, float)) and not isinstance(value, bool)) else "expected a number"
    if t == "integer":
        if _is_int(value) or (isinstance(value, float) and value.is_integer()):
            return None
        return "expected a whole number"
    if t == "boolean":
        return None if isinstance(value, bool) else "expected true or false"
    if t == "date":
        if not isinstance(value, str):
            return "expected a date as YYYY-MM-DD"
        try:
            date.fromisoformat(value[:10])
            if len(value) != 10:
                raise ValueError
        except ValueError:
            return "expected a date as YYYY-MM-DD"
        return None
    if t == "enum":
        allowed = field.get("enum") or []
        return None if value in allowed else f"must be one of: {', '.join(allowed)}"
    return None


def coerce_human_value(field: Dict[str, Any], value: Any) -> Any:
    """Grid cells arrive as strings; coerce to the field's type (raises ValueError)."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    t = field.get("type") or "string"
    try:
        if t == "number" and isinstance(value, str):
            return float(value.replace(",", "")) if any(c in value for c in ".eE") else int(value.replace(",", ""))
        if t == "integer" and isinstance(value, str):
            return int(value.replace(",", ""))
        if t == "boolean" and isinstance(value, str):
            v = value.strip().lower()
            if v in ("true", "yes", "1", "y"):
                return True
            if v in ("false", "no", "0", "n"):
                return False
            raise ValueError
        if t == "date" and isinstance(value, str):
            return value.strip()[:10]
    except ValueError:
        raise ValueError("invalid value for type " + t)
    return value


def normalize_key(field: Optional[Dict[str, Any]], value: Any) -> Optional[str]:
    if field is None or value is None:
        return None
    if isinstance(value, str):
        v = " ".join(value.split()).casefold()
        return v[:512] or None
    return str(value)[:512]


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


# ── agent submission ───────────────────────────────────────────────────────

def _fields_by_name(agent_list: AgentList) -> Dict[str, Dict[str, Any]]:
    return {f["name"]: f for f in (agent_list.fields or [])}


def _key_field(agent_list: AgentList) -> Optional[Dict[str, Any]]:
    if not agent_list.key_field_id:
        return None
    return next((f for f in (agent_list.fields or []) if f["id"] == agent_list.key_field_id), None)


def _schema_errors(instance: Any, schema: Dict[str, Any]) -> List[str]:
    """Path-qualified JSON-Schema errors, descending into anyOf branches.

    A field envelope is ``anyOf[object, null]``; the generic message ("is not
    valid under any of the given schemas") tells the model nothing, so report
    the most relevant sub-error of the object branch instead.
    """
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import best_match

    out: List[str] = []
    for err in sorted(Draft202012Validator(schema).iter_errors(instance), key=lambda e: list(e.absolute_path)):
        e = err
        while e.validator in ("anyOf", "oneOf") and e.context:
            non_null = [c for c in e.context if c.validator != "type" or c.validator_value != "null"]
            e = best_match(non_null or e.context)
        path = ".".join(str(p) for p in e.absolute_path) or "<root>"
        msg = e.message
        if e.validator == "type":
            got = {"dict": "object", "list": "array", "str": "string", "int": "integer", "float": "number",
                   "bool": "boolean", "NoneType": "null"}.get(type(e.instance).__name__, type(e.instance).__name__)
            exp = e.validator_value
            msg = f"expected {'|'.join(exp) if isinstance(exp, list) else exp}, got {got}"
        elif e.validator == "enum":
            allowed = [v for v in (e.validator_value or []) if v is not None]
            msg = f"must be one of: {', '.join(map(str, allowed))}"
        out.append(f"{path}: {msg}")
        if len(out) >= 20:
            break
    return out


def validate_records(agent_list: AgentList, records: Any) -> List[str]:
    """Schema + semantic validation. Returns path-qualified errors (empty = ok)."""
    schema = compile_list_schema(agent_list.fields or [])
    errors = _schema_errors({"records": records}, schema)
    if errors:
        return errors
    errs: List[str] = []
    if len(records) > MAX_RECORDS_PER_CALL:
        return [f"records: at most {MAX_RECORDS_PER_CALL} records per call"]
    by_name = _fields_by_name(agent_list)
    for i, rec in enumerate(records):
        fields = rec.get("fields") or {}
        for name, env in fields.items():
            if env is None:
                continue
            f = by_name[name]
            base = f"records.{i}.fields.{name}"
            msg = check_value(f, env.get("value"))
            if msg:
                errs.append(f"{base}.value: {msg}")
            status = env.get("status")
            if env.get("value") is None and status in ("found", "inferred"):
                errs.append(f"{base}.status: value is null — use status not_found (or ambiguous) instead of {status}")
            if env.get("value") is not None and status == "not_found":
                errs.append(f"{base}.status: not_found must have value null")
            if (agent_list.require_evidence and f.get("method") == "extract" and status == "found"
                    and not any((e.get("quote") or "").strip() for e in (env.get("evidence") or []))):
                errs.append(f"{base}.evidence: this list requires an exact quote for extracted values")
    return errs


def _stored_envelope(env: Dict[str, Any], sources: List[SourceText]) -> Dict[str, Any]:
    evidence = []
    for e in env.get("evidence") or []:
        item = {k: e.get(k) for k in ("kind", "ref", "page", "quote")}
        item["verified"] = bool(e.get("kind") != "query" and e.get("quote")
                                and verify_quote(e.get("quote"), e.get("ref"), sources))
        evidence.append(item)
    value = env.get("value")
    if isinstance(value, float) and value.is_integer():
        value = int(value) if abs(value) < 2**53 else value
    return {
        "value": value,
        "status": env.get("status"),
        "evidence": evidence,
        "note": env.get("note"),
        "source": "agent",
        "updated_at": _now_iso(),
    }


def _same_fact(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> bool:
    """Same value and status. Re-extraction usually re-words the quote/note;
    that refreshes the evidence but is not a change worth a revision."""
    if a is None or b is None:
        return a is b
    return a.get("value") == b.get("value") and a.get("status") == b.get("status")


async def apply_submission(
    db,
    agent_list: AgentList,
    records: List[Dict[str, Any]],
    *,
    report_id: Optional[str],
    tool_execution_id: Optional[str],
    user_id: Optional[str],
    sources: List[SourceText],
) -> Dict[str, Any]:
    """Validate and upsert ``records``. Raises ListValidationError (nothing written)."""
    errors = validate_records(agent_list, records)
    by_name = _fields_by_name(agent_list)
    key_field = _key_field(agent_list)

    # Resolve targets before writing anything so a bad row_id rejects the call.
    plans: List[Tuple[str, Optional[AgentListRow], Dict[str, Any]]] = []
    if not errors:
        seen_keys: Dict[str, int] = {}
        for i, rec in enumerate(records):
            fields = rec.get("fields") or {}
            target: Optional[AgentListRow] = None
            row_id = rec.get("row_id")
            if row_id:
                target = (await db.execute(
                    select(AgentListRow).where(AgentListRow.id == str(row_id), AgentListRow.list_id == agent_list.id)
                )).scalar_one_or_none()
                if target is None:
                    errors.append(f"records.{i}.row_id: no row {row_id} in list '{agent_list.name}'")
                    continue
            key_val = None
            if key_field is not None:
                env = fields.get(key_field["name"])
                key_val = normalize_key(key_field, env.get("value") if env else None)
                if key_val is not None:
                    if key_val in seen_keys:
                        errors.append(
                            f"records.{i}.fields.{key_field['name']}.value: duplicate key in this call "
                            f"(same as records.{seen_keys[key_val]})"
                        )
                        continue
                    seen_keys[key_val] = i
                    if target is None:
                        target = (await db.execute(
                            select(AgentListRow).where(
                                AgentListRow.list_id == agent_list.id, AgentListRow.key_value == key_val
                            )
                        )).scalar_one_or_none()
            if target is None:
                missing = [
                    f["name"] for f in agent_list.fields or []
                    if f.get("required") and (fields.get(f["name"]) is None or fields[f["name"]].get("value") is None)
                ]
                for m in missing:
                    errors.append(f"records.{i}.fields.{m}.value: required for a new row")
            plans.append(("update" if target is not None else "insert", target, rec))
    if errors:
        raise ListValidationError(errors)

    saved: List[Dict[str, Any]] = []
    records_out: List[Dict[str, Any]] = []
    inserted = updated = unchanged = 0
    locked_skipped: List[Dict[str, Any]] = []
    unverified: List[Dict[str, Any]] = []
    name_of = {f["id"]: f["name"] for f in (agent_list.fields or [])}

    def _key_of(row: AgentListRow, envs: Dict[str, Dict[str, Any]]) -> Any:
        if key_field is None:
            return None
        env = envs.get(key_field["id"]) or (row.values or {}).get(key_field["id"]) or {}
        return env.get("value")

    def _record_out(i: int, action: str, row: AgentListRow, envs: Dict[str, Dict[str, Any]],
                    skipped: set) -> None:
        """Per-record detail for the chat card (UI only — not model-visible)."""
        fields_out: Dict[str, Any] = {}
        for fid, env in envs.items():
            ev = next((e for e in env.get("evidence") or [] if e.get("quote")), None)
            fields_out[name_of.get(fid, fid)] = {
                "value": env.get("value"),
                "status": env.get("status"),
                "quote": ev.get("quote") if ev else None,
                "page": ev.get("page") if ev else None,
                "ref": ev.get("ref") if ev else None,
                "verified": ev.get("verified") if ev else None,
                "locked": fid in skipped,
                "note": env.get("note"),
            }
        records_out.append({"record": i, "row_id": row.id, "action": action,
                            "key": _key_of(row, envs), "fields": fields_out})

    for i, (action, target, rec) in enumerate(plans):
        fields = rec.get("fields") or {}
        new_envs: Dict[str, Dict[str, Any]] = {}
        for name, env in fields.items():
            if env is None:
                continue
            f = by_name[name]
            stored = _stored_envelope(env, sources)
            for ev in stored["evidence"]:
                if ev.get("quote") and not ev["verified"]:
                    unverified.append({"record": i, "field": name, "quote": ev["quote"][:160]})
            new_envs[f["id"]] = stored

        if action == "insert":
            row = AgentListRow(
                list_id=agent_list.id,
                key_value=normalize_key(key_field, (fields.get(key_field["name"]) or {}).get("value")) if key_field else None,
                values=new_envs,
                schema_version=agent_list.version,
                row_version=1,
                locked_fields=[],
                report_id=report_id,
                tool_execution_id=tool_execution_id,
                created_by_user_id=user_id,
                updated_by_user_id=user_id,
            )
            db.add(row)
            await db.flush()
            db.add(AgentListRowRevision(
                row_id=row.id, list_id=agent_list.id, actor_type="agent", actor_user_id=user_id,
                report_id=report_id, tool_execution_id=tool_execution_id, action="insert",
                changed={fid: {"before": None, "after": env} for fid, env in new_envs.items()},
            ))
            inserted += 1
            saved.append({"row_id": row.id, "key": _key_of(row, new_envs), "action": "inserted"})
            _record_out(i, "inserted", row, new_envs, set())
            continue

        row = target
        values = copy.deepcopy(row.values or {})
        locked = set(row.locked_fields or [])
        changed: Dict[str, Any] = {}
        refreshed = False
        skipped_ids: set = set()
        for fid, env in new_envs.items():
            if fid in locked:
                skipped_ids.add(fid)
                fname = next((f["name"] for f in agent_list.fields if f["id"] == fid), fid)
                locked_skipped.append({"record": i, "field": fname, "row_id": row.id})
                continue
            before = values.get(fid)
            if _same_fact(before, env):
                if before.get("evidence") != env["evidence"] or before.get("note") != env["note"]:
                    values[fid] = {**before, "evidence": env["evidence"], "note": env["note"]}
                    refreshed = True
                continue
            values[fid] = env
            changed[fid] = {"before": before, "after": env}
        if not changed:
            if refreshed:
                row.values = values
            unchanged += 1
            saved.append({"row_id": row.id, "key": _key_of(row, new_envs), "action": "unchanged"})
            _record_out(i, "unchanged", row, new_envs, skipped_ids)
            continue
        row.values = values
        if key_field is not None and key_field["id"] in changed:
            row.key_value = normalize_key(key_field, values[key_field["id"]].get("value"))
        row.row_version = (row.row_version or 1) + 1
        row.schema_version = agent_list.version
        row.report_id = report_id or row.report_id
        row.tool_execution_id = tool_execution_id
        row.updated_by_user_id = user_id
        db.add(AgentListRowRevision(
            row_id=row.id, list_id=agent_list.id, actor_type="agent", actor_user_id=user_id,
            report_id=report_id, tool_execution_id=tool_execution_id, action="update", changed=changed,
        ))
        updated += 1
        saved.append({"row_id": row.id, "key": _key_of(row, new_envs), "action": "updated"})
        _record_out(i, "updated", row, new_envs, skipped_ids)

    await db.commit()
    return {
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "rows": saved,
        "records": records_out,
        "locked_fields_skipped": locked_skipped,
        "unverified_quotes": unverified,
    }


async def apply_submission_with_retry(db, agent_list: AgentList, records, **kw) -> Dict[str, Any]:
    """Two concurrent runs inserting the same new key: the loser retries as an update."""
    list_id = agent_list.id  # read before any failed flush poisons the session
    try:
        return await apply_submission(db, agent_list, records, **kw)
    except IntegrityError:
        await db.rollback()
        # Rollback expires every loaded instance; reload the list explicitly —
        # a lazy attribute load here would run outside the async greenlet.
        await db.refresh(agent_list)
        logger.info("agent list %s: key conflict on insert, retrying as update", list_id)
        return await apply_submission(db, agent_list, records, **kw)


# ── human edits ────────────────────────────────────────────────────────────

def _resolve_field(agent_list: AgentList, ref: str) -> Optional[Dict[str, Any]]:
    return next((f for f in (agent_list.fields or []) if f["id"] == ref or f["name"] == ref), None)


async def patch_row(db, agent_list: AgentList, row: AgentListRow, *, row_version: int,
                    fields: Dict[str, Any], unlock: List[str], user_id: str) -> AgentListRow:
    if int(row_version) != int(row.row_version or 1):
        raise RowConflictError("This row changed since you loaded it. Reload and try again.")
    errors: List[str] = []
    resolved: Dict[str, Tuple[Dict[str, Any], Any]] = {}
    for ref, raw in (fields or {}).items():
        f = _resolve_field(agent_list, ref)
        if f is None:
            errors.append(f"fields.{ref}: unknown field")
            continue
        try:
            val = coerce_human_value(f, raw)
        except ValueError as exc:
            errors.append(f"fields.{f['name']}: {exc}")
            continue
        msg = check_value(f, val)
        if msg:
            errors.append(f"fields.{f['name']}: {msg}")
            continue
        if val is None and f.get("required"):
            errors.append(f"fields.{f['name']}: required")
            continue
        resolved[f["id"]] = (f, val)
    unlock_ids = []
    for ref in unlock or []:
        f = _resolve_field(agent_list, ref)
        if f is None:
            errors.append(f"unlock.{ref}: unknown field")
        else:
            unlock_ids.append(f["id"])
    if errors:
        raise ListValidationError(errors)

    values = copy.deepcopy(row.values or {})
    locked = list(row.locked_fields or [])
    changed: Dict[str, Any] = {}
    now = _now_iso()
    for fid, (f, val) in resolved.items():
        before = values.get(fid)
        if before is not None and before.get("value") == val and before.get("source") == "human":
            continue
        after = {
            "value": val,
            "status": "found" if val is not None else "not_found",
            "evidence": [dict(e, superseded=True) for e in ((before or {}).get("evidence") or [])],
            "note": (before or {}).get("note"),
            "source": "human",
            "edited_by": user_id,
            "updated_at": now,
        }
        values[fid] = after
        changed[fid] = {"before": before, "after": after}
        if fid not in locked:
            locked.append(fid)
    unlocked = [fid for fid in unlock_ids if fid in locked]
    locked = [fid for fid in locked if fid not in unlocked]

    if not changed and not unlocked:
        return row

    key_field = _key_field(agent_list)
    if key_field is not None and key_field["id"] in changed:
        new_key = normalize_key(key_field, values[key_field["id"]].get("value"))
        if new_key is not None:
            clash = (await db.execute(
                select(AgentListRow.id).where(
                    AgentListRow.list_id == agent_list.id, AgentListRow.key_value == new_key,
                    AgentListRow.id != row.id,
                )
            )).scalar_one_or_none()
            if clash:
                raise RowConflictError(f"Another row already has {key_field['name']} = {values[key_field['id']].get('value')!r}.")
        row.key_value = new_key

    row.values = values
    row.locked_fields = locked
    row.row_version = (row.row_version or 1) + 1
    row.updated_by_user_id = user_id
    if changed:
        db.add(AgentListRowRevision(
            row_id=row.id, list_id=agent_list.id, actor_type="user", actor_user_id=user_id,
            action="update", changed=changed,
        ))
    if unlocked:
        db.add(AgentListRowRevision(
            row_id=row.id, list_id=agent_list.id, actor_type="user", actor_user_id=user_id,
            action="unlock", changed={fid: {"unlocked": True} for fid in unlocked},
        ))
    await db.commit()
    await db.refresh(row)
    return row


async def revert_revision(db, agent_list: AgentList, row: AgentListRow, revision: AgentListRowRevision,
                          user_id: str) -> AgentListRow:
    if revision.action not in ("update", "revert"):
        raise ListValidationError([f"revision: a '{revision.action}' revision cannot be reverted"])
    values = copy.deepcopy(row.values or {})
    locked = list(row.locked_fields or [])
    changed: Dict[str, Any] = {}
    for fid, diff in (revision.changed or {}).items():
        if not isinstance(diff, dict) or "before" not in diff:
            continue
        before_now = values.get(fid)
        restored = diff.get("before")
        if restored is None:
            values.pop(fid, None)
        else:
            values[fid] = dict(restored)
        changed[fid] = {"before": before_now, "after": restored}
        if fid not in locked:
            locked.append(fid)
    if not changed:
        return row
    key_field = _key_field(agent_list)
    if key_field is not None and key_field["id"] in changed:
        row.key_value = normalize_key(key_field, (values.get(key_field["id"]) or {}).get("value"))
    row.values = values
    row.locked_fields = locked
    row.row_version = (row.row_version or 1) + 1
    row.updated_by_user_id = user_id
    db.add(AgentListRowRevision(
        row_id=row.id, list_id=agent_list.id, actor_type="user", actor_user_id=user_id,
        action="revert", changed=changed,
    ))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise RowConflictError("Reverting would duplicate another row's key.")
    await db.refresh(row)
    return row
