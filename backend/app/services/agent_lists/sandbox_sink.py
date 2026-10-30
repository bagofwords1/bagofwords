"""Agent List writes from generated code (`lists.submit(...)` in create_data).

Generated code runs in the sandbox and may be retried, so it never writes to
the database itself. ``SandboxListSink.submit`` is the trusted-side handler
behind the sandbox's ``lists`` helper: it resolves the list, validates the
records with the same rules ``submit_list`` uses, and holds them. Nothing is
saved until ``commit_sandbox_submissions`` runs after the run succeeded, so a
failed attempt (which the executor retries) leaves no rows behind.

The sink only ever exists for the agent's own create_data run: report reruns
and dashboard refreshes build their executor without one, so old code cannot
write to a list again later.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from app.services.agent_lists.records import (
    MAX_RECORDS_PER_CALL,
    normalize_flat_records,
    normalize_not_found,
    validate_records,
)

logger = logging.getLogger(__name__)

# Errors returned to the code (or the model) for one bad batch / chunk.
_MAX_ERRORS_SHOWN = 12


class SandboxListError(ValueError):
    """A `lists.submit` call the code can fix (unknown list, invalid records,
    over the per-run row limit). Raised inside the generated code."""


@dataclass(frozen=True)
class SandboxList:
    """Read-only snapshot of a list the code may write to. Plain data, so the
    sink can validate on the sandbox's RPC thread without touching an ORM
    session."""
    id: str
    name: str
    slug: str
    data_source_id: str
    fields: List[Dict[str, Any]]
    key_field_id: Optional[str]
    require_evidence: bool

    def as_validation_target(self) -> SimpleNamespace:
        return SimpleNamespace(
            id=self.id, name=self.name, fields=self.fields,
            key_field_id=self.key_field_id, require_evidence=self.require_evidence,
        )


@dataclass
class SandboxListSink:
    lists: List[SandboxList]
    max_rows: int
    _pending: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ── lookup ──────────────────────────────────────────────────────────
    def _resolve(self, ref: Any) -> SandboxList:
        r = str(ref or "").strip()
        available = ", ".join(sorted(f"{l.slug!r}" for l in self.lists)) or "none"
        if not r:
            raise SandboxListError(
                f"lists.submit: pass the list's slug or name as the first argument. Available lists: {available}"
            )
        low = r.casefold()
        matches = [l for l in self.lists if r == l.id or low in (l.slug.casefold(), l.name.casefold())]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise SandboxListError(
                f"lists.submit: {r!r} matches more than one list; pass the list id instead. "
                f"Lists: {available}"
            )
        raise SandboxListError(f"lists.submit: no list named {r!r}. Available lists: {available}")

    def names(self) -> List[Dict[str, Any]]:
        """What `lists.available()` returns to the code."""
        out = []
        for l in self.lists:
            key = next((f["name"] for f in l.fields if f.get("id") == l.key_field_id), None)
            out.append({
                "slug": l.slug, "name": l.name, "key": key,
                "fields": [f.get("name") for f in l.fields],
            })
        return out

    # ── called from the sandbox (RPC worker thread) ─────────────────────
    def submit(self, ref: Any, records: Any) -> Dict[str, Any]:
        lst = self._resolve(ref)
        if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
            raise SandboxListError("lists.submit: records must be a list of dicts (one dict per row)")
        if not records:
            return {"list": lst.slug, "queued": 0, "total_queued": self.queued()}
        normalized = normalize_not_found(_web_evidence(normalize_flat_records(records, [f["name"] for f in lst.fields])))
        target = lst.as_validation_target()
        errors: List[str] = []
        for start in range(0, len(normalized), MAX_RECORDS_PER_CALL):
            chunk = normalized[start:start + MAX_RECORDS_PER_CALL]
            for e in validate_records(target, chunk):
                # Re-base "records.<i>" onto the caller's index.
                errors.append(_rebase(e, start))
            if len(errors) >= _MAX_ERRORS_SHOWN:
                break
        if errors:
            raise SandboxListError(
                f"lists.submit({lst.slug!r}): {len(errors)} invalid value(s), nothing queued: "
                + "; ".join(errors[:_MAX_ERRORS_SHOWN])
            )
        with self._lock:
            total = sum(len(v) for v in self._pending.values())
            if total + len(normalized) > self.max_rows:
                raise SandboxListError(
                    f"lists.submit: this run may submit at most {self.max_rows} rows "
                    f"({total} already queued, {len(normalized)} more requested)"
                )
            self._pending.setdefault(lst.id, []).extend(normalized)
            total += len(normalized)
        return {"list": lst.slug, "queued": len(normalized), "total_queued": total}

    # ── lifecycle ───────────────────────────────────────────────────────
    def reset(self) -> None:
        """Drop what a previous (failed) attempt queued."""
        with self._lock:
            self._pending = {}

    def queued(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._pending.values())

    def take(self) -> Dict[str, List[Dict[str, Any]]]:
        with self._lock:
            pending, self._pending = self._pending, {}
            return pending


_EVIDENCE_REF_KEYS = ("ref", "url", "source")


def _web_evidence(records: Any) -> Any:
    """Fill the evidence item shape for code: `{"quote": q, "ref": url}` (or
    `url` / `source`) becomes `{"kind": "web", "ref": url, "page": None,
    "quote": q}`. Items already in the full shape pass through; anything else
    is left for validation to report. Works on the copy normalize_* made."""
    if not isinstance(records, list):
        return records
    for rec in records:
        fields = rec.get("fields") if isinstance(rec, dict) else None
        if not isinstance(fields, dict):
            continue
        for env in fields.values():
            items = env.get("evidence") if isinstance(env, dict) else None
            if not isinstance(items, list):
                continue
            out = []
            for it in items:
                if isinstance(it, str):
                    it = {"quote": it}
                if isinstance(it, dict) and set(it) <= {"kind", "page", "quote", *_EVIDENCE_REF_KEYS}:
                    ref = next((it[k] for k in _EVIDENCE_REF_KEYS if it.get(k) is not None), None)
                    it = {"kind": it.get("kind") or "web", "ref": None if ref is None else str(ref),
                          "page": it.get("page"), "quote": it.get("quote")}
                out.append(it)
            env["evidence"] = out
    return records


def _rebase(error: str, offset: int) -> str:
    if not offset or not error.startswith("records."):
        return error
    head, _, rest = error[len("records."):].partition(".")
    if head.isdigit():
        return f"records.{int(head) + offset}.{rest}" if rest else f"records.{int(head) + offset}"
    return error


def _last_per_key(lst, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One record per key, the last one submitted. A scrape that sees the same
    item twice must not fail its whole chunk on "duplicate key in this call"."""
    from app.services.agent_lists.records import normalize_key

    key_field = next((f for f in (lst.fields or []) if f.get("id") == lst.key_field_id), None)
    if key_field is None:
        return records
    by_key: Dict[str, int] = {}
    out: List[Optional[Dict[str, Any]]] = []
    for rec in records:
        env = (rec.get("fields") or {}).get(key_field["name"]) if not rec.get("row_id") else None
        key = normalize_key(key_field, env.get("value")) if isinstance(env, dict) else None
        if key is not None and key in by_key:
            out[by_key[key]] = None
        if key is not None:
            by_key[key] = len(out)
        out.append(rec)
    return [r for r in out if r is not None]


async def load_sandbox_lists(db, report, user, organization) -> List[SandboxList]:
    """Lists the acting user may submit to in this report — the same set the
    planner gets as submit_<slug> tools (attached agents, view access,
    can_submit_to_list, never in a read-only artifact chat)."""
    from sqlalchemy import select

    from app.models.agent_list import AgentList
    from app.ai.tools.list_tool_registry import build_list_tools

    _descriptors, routing = await build_list_tools(db, report, user, organization)
    ids = [r["list_id"] for r in routing.values()]
    if not ids:
        return []
    rows = (await db.execute(select(AgentList).where(AgentList.id.in_(ids)))).scalars().all()
    out = []
    for l in sorted(rows, key=lambda x: (str(x.data_source_id), x.slug)):
        out.append(SandboxList(
            id=str(l.id), name=l.name, slug=l.slug, data_source_id=str(l.data_source_id),
            fields=list(l.fields or []), key_field_id=l.key_field_id,
            require_evidence=bool(l.require_evidence),
        ))
    return out


async def commit_sandbox_submissions(
    db,
    pending: Dict[str, List[Dict[str, Any]]],
    *,
    report,
    user,
    organization,
    tool_execution_id: Optional[str],
    extra_sources: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """Save what a successful run queued. Access is re-checked at write time.

    Records were already validated when queued, so a chunk normally saves
    whole. If one is still rejected (the list changed meanwhile), its records
    are retried one by one so a single bad row costs only itself.
    """
    from app.models.agent_list import AgentList
    from app.models.data_source import DataSource
    from app.services.agent_lists.access import can_submit_to_list
    from app.services.agent_lists.records import ListValidationError, apply_submission_with_retry
    from app.services.agent_lists.verify import collect_report_texts

    report_id = str(report.id) if report is not None else None
    summary: Dict[str, Any] = {"lists": [], "inserted": 0, "updated": 0, "unchanged": 0,
                               "rejected": 0, "errors": []}
    if not pending:
        return summary
    if report is not None and getattr(report, "report_type", "regular") == "artifact_chat":
        summary["errors"].append("lists are read-only in this conversation; nothing was saved")
        summary["rejected"] = sum(len(v) for v in pending.values())
        return summary

    sources = await collect_report_texts(db, report_id)
    sources = list(extra_sources or []) + sources

    async def _apply(lst, recs) -> Tuple[Dict[str, int], List[str]]:
        try:
            res = await apply_submission_with_retry(
                db, lst, recs, report_id=report_id, tool_execution_id=tool_execution_id,
                user_id=str(user.id) if user is not None else None, sources=sources,
            )
            return res, []
        except ListValidationError as exc:
            # Raised before anything is written; earlier chunks are committed.
            return {}, list(exc.errors)

    for list_id, records in pending.items():
        lst = await db.get(AgentList, list_id)
        entry = {"list_id": list_id, "list_name": getattr(lst, "name", None),
                 "inserted": 0, "updated": 0, "unchanged": 0, "rejected": 0}
        summary["lists"].append(entry)
        ds = await db.get(DataSource, lst.data_source_id) if lst is not None and lst.deleted_at is None else None
        if ds is None or not await can_submit_to_list(db, user, organization, ds, lst):
            entry["rejected"] = len(records)
            summary["rejected"] += len(records)
            summary["errors"].append(f"list {list_id}: no longer available; {len(records)} row(s) not saved")
            continue
        records = _last_per_key(lst, records)
        for start in range(0, len(records), MAX_RECORDS_PER_CALL):
            chunk = records[start:start + MAX_RECORDS_PER_CALL]
            res, errs = await _apply(lst, chunk)
            if errs:
                for rec in chunk:
                    one, one_errs = await _apply(lst, [rec])
                    if one_errs:
                        entry["rejected"] += 1
                        if len(summary["errors"]) < _MAX_ERRORS_SHOWN:
                            summary["errors"].append(f"{lst.slug}: {one_errs[0]}")
                    else:
                        for k in ("inserted", "updated", "unchanged"):
                            entry[k] += int(one.get(k) or 0)
                continue
            for k in ("inserted", "updated", "unchanged"):
                entry[k] += int(res.get(k) or 0)
        for k in ("inserted", "updated", "unchanged", "rejected"):
            summary[k] += entry[k]
    return summary
