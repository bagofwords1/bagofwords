"""Collaborative dashboards (collab01;
docs/feedback-loops/2026-10-23-collaborative-dashboards.md).

A dashboard is edited from several conversations, by several people:

- **Editing conversations.** The dashboard's own `artifacts.report_id`
  counts as one, as it always has; `artifact_conversations` adds the others.
  A conversation counts while it is live (not archived or deleted). Each
  holds its own copies of the queries, steps and charts, and its own
  versions of the dashboard, so the agent tools -- which work on a version
  of their own conversation -- need no change.
- **Joining.** An editor without a conversation of their own gets one, a
  copy of the newest draft (the live release when no draft is left). The
  copy keeps rows only when they are the joiner's own (made in their own
  conversation, or published by them); otherwise every step arrives empty
  and runs again with the joiner's credentials (Fork's hydration), whatever
  the connection. A result never reaches another person.
- **Drafts.** A version with no draft is on the dashboard's implicit draft;
  a named draft is opt-in (`artifact_drafts`).
- **Lineage.** `base_version_id` chains a version to the one it grew from;
  Publish warns when the live release is not in that chain (someone
  published since this draft started).
- **Elements.** Each version records a fingerprint per chart; comparing
  them says who changed which chart.
- **The summary.** A new conversation starts with a summary built from
  structure and history, never from the earlier chat or its rows.
"""
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime

from sqlalchemy import or_, select, update
from sqlalchemy.orm import lazyload, selectinload

from app.errors import AppError, ErrorCode
from app.models.artifact import Artifact, ArtifactVersion
from app.models.artifact_release import ArtifactConversation, ArtifactDraft, ArtifactRelease

logger = logging.getLogger(__name__)

# How many ready releases a dashboard keeps (the live one included): what
# Roll back can go back to.
KEEP_RELEASES = 5

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_MAIN = "main"  # the implicit draft's slug in URLs


# ── people ─────────────────────────────────────────────────────────────────

async def person(db, user_id) -> dict | None:
    from app.models.user import User

    if not user_id:
        return None
    user = await db.get(User, str(user_id))
    if user is None:
        return None
    return {"id": str(user.id), "name": user.name or user.email}


# ── editing conversations ──────────────────────────────────────────────────

async def _live_reports(db, report_ids: list[str]) -> set[str]:
    from app.models.report import Report

    if not report_ids:
        return set()
    return {str(r) for (r,) in (await db.execute(
        select(Report.id).where(Report.id.in_(report_ids), Report.deleted_at.is_(None),
                                Report.status != "archived")
    )).all()}


async def editing_report_ids(db, artifact: Artifact, *, exclude: set[str] | None = None) -> list[str]:
    """The live conversations editing this dashboard: its own first, then
    the joined ones, newest first."""
    ids: list[str] = [str(artifact.report_id)] if artifact.report_id else []
    ids += [str(r) for (r,) in (await db.execute(
        select(ArtifactConversation.report_id).where(
            ArtifactConversation.artifact_id == str(artifact.id), ArtifactConversation.ended_at.is_(None),
            ArtifactConversation.deleted_at.is_(None),
        ).order_by(ArtifactConversation.started_at.desc())
    )).all()]
    ids = [i for i in dict.fromkeys(ids) if not exclude or i not in exclude]
    live = await _live_reports(db, ids)
    return [i for i in ids if i in live]


async def is_editing_conversation(db, artifact: Artifact, report_id) -> bool:
    return report_id is not None and str(report_id) in await editing_report_ids(db, artifact)


async def my_conversation(db, artifact: Artifact, user, *, draft_id: str | None = "*") -> str | None:
    """`user`'s own live conversation editing this dashboard (on `draft_id`
    when given; None is the implicit draft), newest first."""
    from app.models.report import Report

    if user is None:
        return None
    ids = await editing_report_ids(db, artifact)
    if not ids:
        return None
    mine = {str(r) for (r,) in (await db.execute(
        select(Report.id).where(Report.id.in_(ids), Report.user_id == str(user.id))
    )).all()}
    for report_id in ids:
        if report_id not in mine:
            continue
        if draft_id != "*" and await conversation_draft_id(db, artifact, report_id) != draft_id:
            continue
        return report_id
    return None


async def conversation_draft_id(db, artifact: Artifact, report_id: str) -> str | None:
    row = (await db.execute(
        select(ArtifactConversation.draft_id).where(ArtifactConversation.artifact_id == str(artifact.id),
                                                    ArtifactConversation.report_id == str(report_id))
    )).first()
    return str(row[0]) if row and row[0] else None


async def dashboards_of_conversations(db, report_ids: list[str], *, lock: bool = False) -> list[Artifact]:
    """Every live dashboard these conversations edit: as its own conversation,
    or joined."""
    if not report_ids:
        return []
    report_ids = [str(r) for r in report_ids]
    joined = select(ArtifactConversation.artifact_id).where(
        ArtifactConversation.report_id.in_(report_ids), ArtifactConversation.ended_at.is_(None))
    stmt = select(Artifact).where(or_(Artifact.report_id.in_(report_ids), Artifact.id.in_(joined)),
                                  Artifact.deleted_at.is_(None))
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return list((await db.execute(stmt)).scalars().all())


async def end_conversations(db, artifact: Artifact, report_ids: list[str]) -> list[str]:
    """These conversations stop editing the dashboard (they were deleted).
    Returns the conversations still editing it. When its own conversation
    went and others remain, the newest of them becomes its own. Not
    committed."""
    gone = {str(r) for r in report_ids}
    await db.execute(update(ArtifactConversation).where(
        ArtifactConversation.artifact_id == str(artifact.id), ArtifactConversation.report_id.in_(gone),
        ArtifactConversation.ended_at.is_(None)).values(ended_at=datetime.utcnow()))
    remaining = await editing_report_ids(db, artifact, exclude=gone)
    if remaining and (artifact.report_id is None or str(artifact.report_id) in gone):
        await db.execute(update(Artifact).where(Artifact.id == str(artifact.id)).values(report_id=remaining[0]))
    return remaining


# ── drafts ─────────────────────────────────────────────────────────────────

def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:100]
    return slug or f"draft-{uuid.uuid4().hex[:6]}"


async def draft_by_slug(db, artifact: Artifact, slug: str | None) -> ArtifactDraft | None:
    """The named draft `slug` names; None for the implicit one ("main" or
    nothing). An unknown name is not found."""
    if not slug or slug == _MAIN:
        return None
    draft = (await db.execute(
        select(ArtifactDraft).where(ArtifactDraft.artifact_id == str(artifact.id), ArtifactDraft.slug == slug,
                                    ArtifactDraft.deleted_at.is_(None))
    )).scalar_one_or_none()
    if draft is None:
        raise AppError.not_found(ErrorCode.ARTIFACT_NOT_FOUND)
    return draft


async def head_version(db, artifact: Artifact, draft_id: str | None = None, *,
                       light: bool = True) -> ArtifactVersion | None:
    """A draft's newest completed version among the live editing
    conversations (`draft_id` None: the implicit draft)."""
    from app.services.dashboard_data import draft_version

    return await draft_version(db, artifact, light=light, draft_id=draft_id)


async def list_drafts(db, artifact: Artifact) -> list[dict]:
    """The implicit draft, then the named ones still open, each with its
    newest version."""
    out = []
    main = await head_version(db, artifact, None)
    out.append({"id": None, "name": "Main", "slug": _MAIN, "status": "open",
                "head_version_id": str(main.id) if main else None,
                "head_version_number": main.version if main else None,
                "updated_at": main.created_at if main else None, "created_by": None})
    drafts = (await db.execute(
        select(ArtifactDraft).where(ArtifactDraft.artifact_id == str(artifact.id), ArtifactDraft.deleted_at.is_(None),
                                    ArtifactDraft.status != "abandoned").order_by(ArtifactDraft.created_at)
    )).scalars().all()
    for d in drafts:
        head = await head_version(db, artifact, str(d.id))
        out.append({"id": str(d.id), "name": d.name, "slug": d.slug, "status": d.status,
                    "head_version_id": str(head.id) if head else None,
                    "head_version_number": head.version if head else None,
                    "updated_at": head.created_at if head else d.created_at,
                    "created_by": await person(db, d.created_by)})
    return out


async def create_draft(db, artifact: Artifact, user, organization, *, name: str,
                       from_version_id: str | None = None) -> dict:
    """A named draft, started in a new conversation of `user` from
    `from_version_id` (default: the implicit draft's newest version)."""
    slug = _slugify(name)
    if slug == _MAIN or (await db.execute(
            select(ArtifactDraft.id).where(ArtifactDraft.artifact_id == str(artifact.id),
                                           ArtifactDraft.slug == slug))).first():
        raise AppError.conflict(ErrorCode.DASHBOARD_DRAFT_NAME_TAKEN, "A draft with this name already exists")
    source = None
    if from_version_id:
        source = (await db.execute(
            select(ArtifactVersion).options(lazyload("*")).where(
                ArtifactVersion.id == str(from_version_id), ArtifactVersion.artifact_id == str(artifact.id),
                ArtifactVersion.status == "completed", ArtifactVersion.deleted_at.is_(None))
        )).scalar_one_or_none()
        if source is None:
            raise AppError.not_found(ErrorCode.ARTIFACT_NOT_FOUND)
    draft = ArtifactDraft(artifact_id=str(artifact.id), organization_id=str(artifact.organization_id),
                          name=name.strip()[:120], slug=slug, status="open", created_by=str(user.id))
    db.add(draft)
    await db.flush()
    result = await join(db, artifact, user, organization, draft=draft, source_version=source, commit=False)
    await db.commit()
    result["draft"] = {"id": str(draft.id), "name": draft.name, "slug": draft.slug}
    return result


# ── lineage and "published since" ──────────────────────────────────────────

async def _version_rows(db, artifact_id: str) -> dict:
    rows = (await db.execute(
        select(ArtifactVersion.id, ArtifactVersion.version, ArtifactVersion.report_id,
               ArtifactVersion.base_version_id, ArtifactVersion.user_id, ArtifactVersion.created_at,
               ArtifactVersion.elements, ArtifactVersion.status, ArtifactVersion.draft_id,
               ArtifactVersion.name)
        .where(ArtifactVersion.artifact_id == str(artifact_id))
    )).all()
    return {str(r.id): r for r in rows}


def _lineage(rows: dict, version_id: str) -> list:
    """`version_id` and every version it grew from, newest first: the
    base_version_id chain, else the previous version of the same
    conversation (versions made before collab01 carry no base)."""
    out, seen = [], set()
    by_report: dict = {}
    for r in rows.values():
        by_report.setdefault(str(r.report_id), []).append(r)
    cur = rows.get(str(version_id))
    while cur is not None and str(cur.id) not in seen and len(out) < 1000:
        seen.add(str(cur.id))
        out.append(cur)
        if cur.base_version_id and str(cur.base_version_id) in rows:
            cur = rows[str(cur.base_version_id)]
            continue
        older = [r for r in by_report.get(str(cur.report_id), []) if r.version < cur.version]
        cur = max(older, key=lambda r: r.version) if older else None
    return out


async def lineage_ids(db, version: ArtifactVersion) -> list[str]:
    return [str(r.id) for r in _lineage(await _version_rows(db, str(version.artifact_id)), str(version.id))]


async def published_since(db, artifact: Artifact, version: ArtifactVersion) -> dict | None:
    """Who published, and when, a live release this version does not build
    on -- None when the version grew from what is live (or nothing is)."""
    from app.services.dashboard_access import published_release

    live = await published_release(db, artifact)
    if live is None or not live.source_version_id:
        return None
    if str(live.source_version_id) in await lineage_ids(db, version):
        return None
    return {"by": await person(db, live.published_by), "at": live.published_at, "release_id": str(live.id)}


# ── elements: who changed which chart ──────────────────────────────────────

def _fingerprint(code, view, title, parameters) -> str:
    """Stable across copies: ids (which every copy rewrites) are masked."""
    params = []
    for spec in parameters or []:
        if isinstance(spec, dict):
            spec = {k: v for k, v in spec.items() if k != "options_source"}
        params.append(spec)
    raw = json.dumps([code or "", view or {}, title or "", params], sort_keys=True, default=str)
    return hashlib.sha1(_UUID.sub("<id>", raw).encode("utf-8")).hexdigest()[:16]


def element_key(viz) -> str:
    return str(viz.element_key or viz.id)


async def snapshot_elements(db, version: ArtifactVersion) -> dict:
    """{element_key: {"fp", "title"}} for each chart this version shows."""
    from app.models.query import Query
    from app.services.dashboard_copy import collect_dashboard_deps

    deps = await collect_dashboard_deps(db, version, include_pending=True)
    if not deps.query_ids:
        return {}
    listed = {str(v) for v in ((version.content or {}).get("visualization_ids") or [])}
    queries = (await db.execute(
        select(Query).options(lazyload("*"), selectinload(Query.default_step).options(lazyload("*")),
                              selectinload(Query.visualizations).options(lazyload("*")))
        .where(Query.id.in_(deps.query_ids))
    )).scalars().unique().all()
    out = {}
    for q in queries:
        code = q.default_step.code if q.default_step is not None else ""
        for v in q.visualizations:
            if v.deleted_at is not None or (listed and str(v.id) not in listed):
                continue
            out[element_key(v)] = {"fp": _fingerprint(code, v.view, v.title, q.parameters),
                                   "title": v.title or q.title or ""}
    return out


async def changes(db, artifact: Artifact, version: ArtifactVersion) -> dict:
    """For each chart of `version`: the version, person and time it last
    changed, and how it compares with the live release."""
    from app.services.dashboard_access import published_release

    rows = await _version_rows(db, str(artifact.id))
    lineage = _lineage(rows, str(version.id))
    elements = (rows[str(version.id)].elements if str(version.id) in rows else None)
    if elements is None:
        elements = await snapshot_elements(db, version)
    live = await published_release(db, artifact)
    live_rows = rows.get(str(live.source_version_id)) if live is not None and live.source_version_id else None
    live_elements = (live_rows.elements or {}) if live_rows is not None else None
    people: dict = {}

    async def who(user_id):
        if user_id not in people:
            people[user_id] = await person(db, user_id)
        return people[user_id]

    out = []
    for key, el in elements.items():
        changed = lineage[0] if lineage else None
        for older in lineage[1:]:
            previous = (older.elements or {}).get(key)
            if previous is None or previous.get("fp") != el.get("fp"):
                break
            changed = older
        if live_elements is None:
            vs_live = "added" if live is None else "unchanged"
        elif key not in live_elements:
            vs_live = "added"
        elif live_elements[key].get("fp") != el.get("fp"):
            vs_live = "changed"
        else:
            vs_live = "unchanged"
        out.append({"key": key, "title": el.get("title"),
                    "changed_by": await who(changed.user_id) if changed is not None else None,
                    "changed_at": changed.created_at if changed is not None else None,
                    "version_number": changed.version if changed is not None else None,
                    "vs_live": vs_live})
    removed = [{"key": k, "title": e.get("title"), "vs_live": "removed"}
               for k, e in (live_elements or {}).items() if k not in elements]
    return {"version_id": str(version.id), "elements": out, "removed_since_live": removed,
            "live_version_id": str(live.source_version_id) if live is not None and live.source_version_id else None,
            "published_since": await published_since(db, artifact, version)}


# ── the summary a new conversation starts with ─────────────────────────────

async def build_summary(db, artifact: Artifact, *, source_version: ArtifactVersion | None,
                        release: ArtifactRelease | None) -> dict:
    """Built in code from structure and history, never from the earlier chat
    or its rows (they were produced with someone else's credentials): the
    charts by title with their sources and filters, the notes, and the
    change log of the versions this one grows from."""
    from app.models.data_source import DataSource
    from app.models.query import Query
    from app.models.report_data_source_association import report_data_source_association as assoc
    from app.services.dashboard_copy import collect_dashboard_deps

    charts, sources = [], []
    if source_version is not None:
        deps = await collect_dashboard_deps(db, source_version)
        query_ids = deps.query_ids
        ds_ids = select(assoc.c.data_source_id).where(assoc.c.report_id == str(source_version.report_id))
    elif release is not None:
        from app.models.artifact_release import artifact_release_data_source_association as rassoc

        query_ids = [str(q) for (q,) in (await db.execute(
            select(Query.id).where(Query.artifact_release_id == str(release.id), Query.deleted_at.is_(None))
        )).all()]
        ds_ids = select(rassoc.c.data_source_id).where(rassoc.c.artifact_release_id == str(release.id))
    else:
        query_ids, ds_ids = [], None
    if ds_ids is not None:
        sources = [n for (n,) in (await db.execute(
            select(DataSource.name).where(DataSource.id.in_(ds_ids), DataSource.deleted_at.is_(None))
        )).all()]
    if query_ids:
        queries = (await db.execute(
            select(Query).options(lazyload("*"), selectinload(Query.visualizations).options(lazyload("*")))
            .where(Query.id.in_(query_ids)).order_by(Query.created_at)
        )).scalars().unique().all()
        for q in queries:
            filters = [str(p.get("name") or p.get("label")) for p in (q.parameters or [])
                       if isinstance(p, dict) and (p.get("name") or p.get("label"))]
            for v in q.visualizations:
                if v.deleted_at is None:
                    charts.append({"title": v.title or q.title or "", "query": q.title or "", "filters": filters})

    history = []
    if source_version is not None:
        rows = await _version_rows(db, str(artifact.id))
        lineage = _lineage(rows, str(source_version.id))
        for newer, older in zip(lineage, lineage[1:] + [None]):
            if len(history) >= 10:
                break
            new_el, old_el = newer.elements or {}, (older.elements or {}) if older is not None else {}
            added = [e.get("title") for k, e in new_el.items() if k not in old_el]
            removed = [e.get("title") for k, e in old_el.items() if k not in new_el]
            changed = [e.get("title") for k, e in new_el.items() if k in old_el and old_el[k].get("fp") != e.get("fp")]
            if older is not None and not (added or removed or changed):
                continue
            who = await person(db, newer.user_id)
            history.append({"version": newer.version, "by": who["name"] if who else None,
                            "at": newer.created_at.isoformat() if newer.created_at else None,
                            "added": added, "changed": changed, "removed": removed})
    previous = None
    if source_version is not None:
        from app.models.report import Report

        owner = (await db.execute(select(Report.user_id).where(Report.id == str(source_version.report_id)))).first()
        previous = await person(db, owner[0]) if owner else None
    elif release is not None:
        previous = await person(db, release.published_by)
    return {"dashboard": {"id": str(artifact.id), "title": artifact.title},
            "notes": artifact.notes, "continued_from": previous,
            "from_version": source_version.version if source_version is not None else None,
            "from_release": str(release.id) if (source_version is None and release is not None) else None,
            "charts": charts, "sources": sources, "history": history}


def summary_text(summary: dict | None, notes: str | None = None) -> str:
    """The summary as the agent reads it."""
    if not summary and not notes:
        return ""
    summary = summary or {}
    lines = []
    prev = (summary.get("continued_from") or {}).get("name")
    if prev:
        frm = f" (version {summary['from_version']})" if summary.get("from_version") else ""
        lines.append(f"This conversation continues work on the dashboard that {prev} edited{frm}. "
                     "Its charts were copied here and run again with this user's own credentials; "
                     "the earlier conversation and its results are not available.")
    notes = notes if notes is not None else summary.get("notes")
    if notes:
        lines.append("Dashboard notes (written by its editors):\n" + str(notes).strip())
    if summary.get("charts"):
        lines.append("Charts: " + "; ".join(
            c["title"] + (f" (filters: {', '.join(c['filters'])})" if c.get("filters") else "")
            for c in summary["charts"][:40]))
    if summary.get("sources"):
        lines.append("Data sources: " + ", ".join(summary["sources"]))
    for h in summary.get("history") or []:
        parts = [f"added {', '.join(h['added'])}" if h.get("added") else "",
                 f"changed {', '.join(h['changed'])}" if h.get("changed") else "",
                 f"removed {', '.join(h['removed'])}" if h.get("removed") else ""]
        parts = [p for p in parts if p]
        lines.append(f"v{h['version']} by {h.get('by') or 'someone'}: " + ("; ".join(parts) or "first version"))
    if summary.get("history"):
        lines.append("(This change log answers questions about what changed in the dashboard and who changed it.)")
    return "\n".join(lines)


async def agent_context(db, report_id: str, artifact_id: str) -> str:
    """What the agent of this conversation is told about the dashboard
    beyond its code: the summary it started from and the current notes."""
    artifact = await db.get(Artifact, str(artifact_id))
    if artifact is None:
        return ""
    row = (await db.execute(
        select(ArtifactConversation.summary).where(ArtifactConversation.artifact_id == str(artifact_id),
                                                   ArtifactConversation.report_id == str(report_id))
    )).first()
    return summary_text(row[0] if row else None, artifact.notes)


# ── joining: a conversation of the editor's own ────────────────────────────

async def check_join_access(db, artifact: Artifact, user, *, ds_ids: list[str], bow_source_access) -> None:
    """A joiner whose charts run again with their credentials must be able
    to use every data source the dashboard runs on, and read its
    monitoring data -- as Fork asks."""
    from app.core.permission_resolver import user_can_access_data_source
    from app.models.data_source import DataSource
    from app.services.bow_source_access import assert_read

    await assert_read(db, bow_source_access, user)
    if not ds_ids:
        return
    sources = (await db.execute(
        select(DataSource).where(DataSource.id.in_(ds_ids), DataSource.deleted_at.is_(None))
    )).scalars().all()
    for ds in sources:
        if not await user_can_access_data_source(db, str(user.id), str(artifact.organization_id), ds):
            raise AppError.forbidden(ErrorCode.DASHBOARD_JOIN_NO_ACCESS,
                                     f"You need access to {ds.name} to edit this dashboard", name=ds.name)


def _rerun_overrides(old_step, old_query) -> dict:
    """A joiner's copy of someone else's step: the code (theirs to edit now),
    no rows, 'pending' until it runs with their credentials."""
    from app.services.fork_service import FORK_PENDING_STATUS

    has_code = bool((old_step.code or "").strip())
    return {"data": {}, "status": FORK_PENDING_STATUS if has_code else old_step.status,
            "status_reason": None}


async def join(db, artifact: Artifact, user, organization, *, draft: ArtifactDraft | None = None,
               source_version: ArtifactVersion | None = None, commit: bool = True,
               into=None, prefer_live: bool = False) -> dict:
    """`user`'s own conversation editing this dashboard (on `draft`): theirs
    if they have one, else a new copy of the draft's newest version -- or,
    with no draft left, of the live release. Returns {report_id, created,
    rerunning, pending_code, version_id, version}; with pending_code the
    caller spawns fork_service.hydrate_fork(..., retire=False).

    `into` (a conversation of `user`): bring the copy there instead -- the
    dashboard in a conversation that already exists, as a new version based
    on `source_version` (or, with `prefer_live` or no draft, the live
    release). A person may edit one dashboard in several conversations."""
    from app.models.report import Report
    from app.models.report_data_source_association import report_data_source_association as assoc
    from app.services.artifact_service import new_version
    from app.services.dashboard_access import published_release
    from app.services.dashboard_lifecycle import (
        _content_for,
        _copy_thumbnail,
        _release_queries,
        _remove_edit_files,
        release_to_conversation,
    )

    draft_id = str(draft.id) if draft is not None else None
    if into is not None and str(into.user_id) != str(user.id):
        raise AppError.forbidden()
    live_first = into is not None and prefer_live and source_version is None \
        and await published_release(db, artifact) is not None
    if source_version is None and not live_first:
        if into is None:
            existing = await my_conversation(db, artifact, user, draft_id=draft_id)
            if existing is not None:
                return {"report_id": existing, "created": False, "rerunning": False, "pending_code": {}}
        source_version = await head_version(db, artifact, draft_id, light=False)
        if source_version is None and draft_id is not None:
            source_version = await head_version(db, artifact, None, light=False)
    release = await published_release(db, artifact) if source_version is None else None
    if source_version is None and release is None:
        raise AppError.conflict(ErrorCode.DASHBOARD_EDIT_FIRST, "There is nothing to edit yet")

    if source_version is not None:
        src_report = await db.get(Report, str(source_version.report_id))
        rows_owner = str(src_report.user_id) if src_report is not None else None
        ds_ids = [str(i) for (i,) in (await db.execute(
            select(assoc.c.data_source_id).where(assoc.c.report_id == str(source_version.report_id)))).all()]
        monitoring = src_report.bow_source_access if src_report is not None else None
    else:
        from app.models.artifact_release import artifact_release_data_source_association as rassoc

        runner = release.run_by or release.published_by  # whose run the stored rows are
        rows_owner = str(runner) if runner else None
        ds_ids = [str(i) for (i,) in (await db.execute(
            select(rassoc.c.data_source_id).where(rassoc.c.artifact_release_id == str(release.id)))).all()]
        monitoring = release.bow_source_access
    own_rows = rows_owner == str(user.id)
    if not own_rows:
        await check_join_access(db, artifact, user, ds_ids=ds_ids, bow_source_access=monitoring)

    title = artifact.title or "Untitled"
    if source_version is not None:
        report, maps = await version_to_conversation(
            db, artifact, source_version, user=user, title=title, project_id=artifact.project_id,
            step_overrides=None if own_rows else _rerun_overrides, link_files=own_rows, into=into)
        stem = f"edit-{report.id}"
        content, deck, previews = _content_for(_VersionAsRelease(source_version), maps, stem, baked=own_rows)
        base_version_id = str(source_version.id)
        thumb_source = None
    else:
        report, maps = await release_to_conversation(
            db, artifact, release, user=user, title=title, project_id=artifact.project_id, slug_prefix="edit",
            step_overrides=None if own_rows else _rerun_overrides, link_files=own_rows, into=into)
        stem = f"edit-{report.id}"
        content, deck, previews = _content_for(release, maps, stem, baked=own_rows)
        base_version_id = str(release.source_version_id) if release.source_version_id else None
        thumb_source = release
    latest = (await db.execute(
        select(ArtifactVersion).where(ArtifactVersion.artifact_id == str(artifact.id))
        .order_by(ArtifactVersion.version.desc()).limit(1)
    )).scalars().first()
    thumbnail = None
    try:
        # The build spec (the request and every edit's note) comes along:
        # it is the design brief, never rows.
        spec_source = source_version if source_version is not None else (
            await db.get(ArtifactVersion, str(release.source_version_id)) if release.source_version_id else None)
        version = await new_version(db, latest, content=content, user_id=str(user.id), report_id=str(report.id),
                                    status="completed", base_version_id=base_version_id, draft_id=draft_id,
                                    generation_prompt=getattr(spec_source, "generation_prompt", None),
                                    **({"pptx_path": deck} if deck else {}))
        # The charts are the source's, unchanged: their fingerprints carry
        # over, so "changed by" keeps pointing at whoever really changed them.
        source_elements = await _source_elements(db, source_version, release)
        if source_elements:
            version.elements = source_elements
        if own_rows:
            # The thumbnail pictures the rows: only the joiner's own carry over.
            if thumb_source is not None:
                thumbnail = _copy_thumbnail(thumb_source, str(version.id), previews)
            elif source_version.thumbnail_path:
                thumbnail = previews.get(source_version.thumbnail_path) or _copy_version_thumbnail(
                    str(source_version.id), str(version.id))
            if thumbnail:
                version.thumbnail_path = thumbnail
        summary = await build_summary(db, artifact, source_version=source_version, release=release)
        row = (await db.execute(select(ArtifactConversation).where(
            ArtifactConversation.artifact_id == str(artifact.id),
            ArtifactConversation.report_id == str(report.id)))).scalars().first()
        if row is not None:
            # Brought again into a conversation that already edits it.
            row.base_version_id, row.summary, row.draft_id = base_version_id, summary, draft_id
            row.started_at, row.ended_at = datetime.utcnow(), None
        elif str(report.id) != str(artifact.report_id or ""):
            db.add(ArtifactConversation(artifact_id=str(artifact.id), report_id=str(report.id), user_id=str(user.id),
                                        draft_id=draft_id, base_version_id=base_version_id, summary=summary,
                                        started_at=datetime.utcnow()))
        if artifact.report_id is None:
            # A published dashboard whose conversation was deleted: this one
            # is its own now (as the owner's Edit makes it).
            await db.execute(update(Artifact).where(Artifact.id == str(artifact.id), Artifact.report_id.is_(None))
                             .values(report_id=str(report.id)))
        await db.flush()
    except BaseException:
        _remove_edit_files(stem, deck, thumbnail)
        raise
    pending = {}
    from app.services.fork_service import FORK_PENDING_STATUS

    for old_step_id, new_step_id in maps.step_map.items():
        code, status = (await db.execute(
            select(_step_code_column(), _step_status_column()).where(_step_id_column() == new_step_id))).one()
        # Someone else's rows re-run as the joiner; so does a step still
        # pending in the source (a copy that was itself re-running).
        if (code or "").strip() and (not own_rows or status == FORK_PENDING_STATUS):
            pending[new_step_id] = code
    pending = await _in_dependency_order(db, str(report.id), pending)
    if commit:
        await db.commit()
    await _audit(db, artifact, user, "dashboard.brought" if into is not None else "dashboard.joined",
                 {"report_id": str(report.id), "draft_id": draft_id, "rerun": not own_rows,
                  "from_version_id": base_version_id})
    return {"report_id": str(report.id), "created": into is None, "rerunning": bool(pending), "pending_code": pending,
            "version_id": str(version.id), "version": version.version}


async def open_state(db, artifact: Artifact, report_id: str) -> dict:
    """What a conversation should open on (collab01): the live version, this
    conversation's newest, whether that newest is behind live (live is not
    in its lineage), and whether it holds work of its own nobody published
    (made here, not a copy brought in, never published). Behind with no
    such work: bring live in. Behind with it: ask."""
    from app.services.dashboard_access import published_release

    live = await published_release(db, artifact)
    live_src = str(live.source_version_id) if live is not None and live.source_version_id else None
    rows = await _version_rows(db, str(artifact.id))
    here = sorted((r for r in rows.values() if str(r.report_id) == str(report_id) and r.status == "completed"),
                  key=lambda r: r.version, reverse=True)
    newest = here[0] if here else None
    behind = bool(live_src) and (newest is None or live_src not in {str(r.id) for r in _lineage(rows, str(newest.id))})
    unpublished = False
    if newest is not None and behind:
        published = {str(v) for (v,) in (await db.execute(select(ArtifactRelease.source_version_id).where(
            ArtifactRelease.artifact_id == str(artifact.id), ArtifactRelease.status == "ready",
            ArtifactRelease.deleted_at.is_(None)))).all() if v}
        base = rows.get(str(newest.base_version_id)) if newest.base_version_id else None
        # A copy brought in grows from another conversation's version; work
        # made here grows from this conversation's (or from nothing).
        made_here = base is None or str(base.report_id) == str(report_id)
        unpublished = made_here and str(newest.id) not in published
    live_row = rows.get(live_src) if live_src else None
    return {
        "live": {"version_id": live_src, "version": live_row.version if live_row is not None else None,
                 "by": await person(db, live.published_by), "at": live.published_at} if live_src else None,
        "newest_here": {"version_id": str(newest.id), "version": newest.version} if newest is not None else None,
        "behind_live": behind, "unpublished_here": unpublished,
    }


async def _source_elements(db, source_version, release) -> dict | None:
    if source_version is None and release is not None and release.source_version_id:
        source_version = await db.get(ArtifactVersion, str(release.source_version_id))
    if source_version is None:
        return None
    return source_version.elements if source_version.elements else await snapshot_elements(db, source_version)


def _copy_version_thumbnail(source_version_id: str, version_id: str) -> str | None:
    try:
        from app.services.thumbnail_service import ThumbnailService
        return ThumbnailService().copy_thumbnail(source_version_id, version_id)
    except Exception:
        return None


def _step_code_column():
    from app.models.step import Step
    return Step.code


def _step_id_column():
    from app.models.step import Step
    return Step.id


def _step_status_column():
    from app.models.step import Step
    return Step.status


async def _in_dependency_order(db, report_id: str, pending: dict) -> dict:
    """`pending` ({step_id: code}) reordered so a step re-runs after the
    steps of this conversation it loads (load_step by id, slug or title, as
    the run resolves it); otherwise in the given order. A cycle keeps the
    given order -- its load_step fails at run time, as it would anyway."""
    if len(pending) < 2:
        return pending
    from app.ai.code_execution.loadables import extract_loadable_refs
    from app.models.query import Query
    from app.models.step import Step

    rows = (await db.execute(
        select(Step.id, Step.slug, Step.title).join(Query, Query.default_step_id == Step.id)
        .where(Query.report_id == str(report_id), Query.deleted_at.is_(None))
        .order_by(Step.created_at.desc())
    )).all()
    by_id, by_slug, by_title = {}, {}, {}
    for sid, slug, title in rows:
        by_id[str(sid)] = str(sid)
        if slug:
            by_slug.setdefault(slug.lower(), str(sid))
        if title:
            by_title.setdefault(title.lower(), str(sid))

    def lookup(ref):
        key = str(ref)
        return by_id.get(key) or by_slug.get(key.lower()) or by_title.get(key.lower())

    ordered, done, visiting = [], set(), set()

    def visit(sid: str) -> None:
        if sid in done or sid in visiting:
            return
        visiting.add(sid)
        for ref in extract_loadable_refs(pending.get(sid) or "")[0]:
            dep = lookup(ref)
            if dep is not None and dep in pending:
                visit(dep)
        visiting.discard(sid)
        done.add(sid)
        ordered.append(sid)

    for sid in list(pending):
        visit(sid)
    return {sid: pending[sid] for sid in ordered}


class _VersionAsRelease:
    """A version in the shape _content_for / _copy_thumbnail read off a
    release: its content, deck, previews' folder and thumbnail."""

    def __init__(self, version: ArtifactVersion):
        self.id = str(version.id)
        self.content = version.content or {}
        self.pptx_path = version.pptx_path
        self.thumbnail_path = version.thumbnail_path


async def version_to_conversation(db, artifact: Artifact, version: ArtifactVersion, *, user, title: str,
                                  project_id: str | None, step_overrides=None, link_files: bool = False,
                                  into=None):
    """A new conversation of `user` holding a copy of what `version` needs
    from its own conversation: the queries it shows and those it reaches,
    their default steps and charts, its sources and theme, its monitoring
    access -- and its files only with `link_files` (the joiner's own)."""
    from app.models.query import Query
    from app.models.report import Report
    from app.services.dashboard_copy import collect_dashboard_deps
    from app.services.dashboard_lifecycle import rows_to_conversation

    src = (await db.execute(
        select(Report).options(lazyload("*"), selectinload(Report.data_sources).options(lazyload("*")),
                               selectinload(Report.files).options(lazyload("*")))
        .where(Report.id == str(version.report_id))
    )).scalar_one()
    # Pending queries too: the source may be a copy still re-running for its
    # own editor; its queries are the dashboard's all the same.
    deps = await collect_dashboard_deps(db, version, include_pending=True)
    loaded = {str(q.id): q for q in (await db.execute(
        select(Query).options(lazyload("*"), selectinload(Query.default_step).options(lazyload("*")),
                              selectinload(Query.visualizations).options(lazyload("*")))
        .where(Query.id.in_(deps.all_query_ids))
    )).scalars().unique().all()}
    return await rows_to_conversation(
        db, artifact, queries=[loaded[i] for i in deps.all_query_ids if i in loaded],
        data_source_ids=[str(ds.id) for ds in src.data_sources],
        file_ids=[str(f.id) for f in src.files] if link_files else [],
        theme_name=src.theme_name, theme_overrides=src.theme_overrides,
        bow_source_access=src.bow_source_access, user=user, title=title, project_id=project_id,
        slug_prefix="edit", step_overrides=step_overrides,
        option_target=lambda q, ref: deps.option_targets.get((str(q.id), ref)), into=into,
    )


# ── history, rollback, transfer ────────────────────────────────────────────

async def history(db, artifact: Artifact, user) -> dict:
    from sqlalchemy import func

    from app.models.report import Report
    from app.services.dashboard_access import published_release

    rows = (await db.execute(
        select(ArtifactConversation).where(ArtifactConversation.artifact_id == str(artifact.id))
        .order_by(ArtifactConversation.started_at)
    )).scalars().all()
    drafts = {str(d.id): d for d in (await db.execute(
        select(ArtifactDraft).where(ArtifactDraft.artifact_id == str(artifact.id)))).scalars().all()}
    counts = dict((await db.execute(
        select(ArtifactVersion.report_id, func.count(ArtifactVersion.id))
        .where(ArtifactVersion.artifact_id == str(artifact.id), ArtifactVersion.deleted_at.is_(None))
        .group_by(ArtifactVersion.report_id)
    )).all())
    live_ids = set(await editing_report_ids(db, artifact))
    conversations = []
    joined = {str(r.report_id): r for r in rows}
    report_ids = list(dict.fromkeys(
        ([str(artifact.report_id)] if artifact.report_id else []) + [str(r) for r in counts] + list(joined)))
    reports = {str(r.id): r for r in (await db.execute(
        select(Report).options(lazyload("*")).where(Report.id.in_(report_ids)))).scalars().all()} if report_ids else {}
    for rid in report_ids:
        report = reports.get(rid)
        if report is None:
            continue
        row = joined.get(rid)
        draft = drafts.get(str(row.draft_id)) if row is not None and row.draft_id else None
        conversations.append({
            "report_id": rid, "user": await person(db, report.user_id),
            "started_at": row.started_at if row is not None else report.created_at,
            "ended_at": (row.ended_at if row is not None else None) or (
                None if rid in live_ids else (report.updated_at if report.status == "archived" else None)),
            "versions": int(counts.get(rid, 0)), "draft": draft.name if draft else None,
            "can_open": user is not None and str(report.user_id) == str(user.id) and rid in live_ids,
        })
    conversations.sort(key=lambda c: c["started_at"] or datetime.min)

    live = await published_release(db, artifact)
    vrows = await _version_rows(db, str(artifact.id))
    releases = []
    for r in (await db.execute(
            select(ArtifactRelease).options(lazyload("*")).where(
                ArtifactRelease.artifact_id == str(artifact.id), ArtifactRelease.deleted_at.is_(None))
            .order_by(ArtifactRelease.created_at.desc()))).scalars().all():
        src = vrows.get(str(r.source_version_id)) if r.source_version_id else None
        releases.append({"id": str(r.id), "status": r.status, "live": live is not None and str(live.id) == str(r.id),
                         "version_id": str(r.source_version_id) if r.source_version_id else None,
                         "version_number": src.version if src is not None else None,
                         "published_by": await person(db, r.published_by), "published_at": r.published_at,
                         "error": r.error})
    # Per version: its kept publishes (newest first). Several publishes of
    # one version are one row ("published 2x"); Restore goes back to the
    # newest kept one, instantly.
    publishes: dict = {}
    for r in releases:
        if r["version_id"] and r["status"] == "ready":
            publishes.setdefault(str(r["version_id"]), []).append(r)
    live_version = str(live.source_version_id) if live is not None and live.source_version_id else None
    newest_first = [v for v in sorted(vrows.values(), key=lambda r: r.version, reverse=True) if v.status == "completed"]
    shown = newest_first[:60]
    # The live version is listed even when it is older than the newest 60.
    if live_version and live_version in vrows and all(str(v.id) != live_version for v in shown):
        shown.append(vrows[live_version])
    versions = []
    for v in shown:
        draft = drafts.get(str(v.draft_id)) if v.draft_id else None
        kept = publishes.get(str(v.id), [])
        is_live = str(v.id) == live_version
        versions.append({"id": str(v.id), "number": v.version, "name": v.name, "author": await person(db, v.user_id),
                         "created_at": v.created_at, "report_id": str(v.report_id),
                         "draft": draft.name if draft else None, "published": bool(kept), "live": is_live,
                         "published_at": max((r["published_at"] for r in kept if r["published_at"]), default=None),
                         "publish_count": len(kept),
                         "restore_release_id": None if is_live or not kept else kept[0]["id"]})
    return {"conversations": conversations, "releases": releases, "versions": versions,
            "drafts": await list_drafts(db, artifact)}


async def name_version(db, artifact: Artifact, version_id: str, name: str) -> dict:
    """Give a version of this dashboard a name ("" removes it). Versions do
    not change once saved; the name is a label on one."""
    version = (await db.execute(select(ArtifactVersion).options(lazyload("*")).where(
        ArtifactVersion.id == str(version_id), ArtifactVersion.artifact_id == str(artifact.id),
        ArtifactVersion.deleted_at.is_(None)))).scalars().first()
    if version is None:
        raise AppError.not_found(ErrorCode.ARTIFACT_NOT_FOUND)
    version.name = (name or "").strip()[:120] or None
    await db.commit()
    return {"id": str(version.id), "number": version.version, "name": version.name}


async def rollback(db, artifact: Artifact, user, organization, release_id: str) -> None:
    """The live release goes back to a kept one: instant, nothing runs, the
    rows are what they were."""
    locked = (await db.execute(
        select(Artifact).where(Artifact.id == str(artifact.id)).with_for_update()
        .execution_options(populate_existing=True))).scalar_one()
    release = (await db.execute(
        select(ArtifactRelease).options(lazyload("*")).where(
            ArtifactRelease.id == str(release_id), ArtifactRelease.artifact_id == str(artifact.id),
            ArtifactRelease.status == "ready", ArtifactRelease.deleted_at.is_(None))
    )).scalar_one_or_none()
    if release is None:
        raise AppError.not_found(ErrorCode.DASHBOARD_RELEASE_NOT_KEPT, "That version is no longer kept")
    previous = str(locked.published_release_id) if locked.published_release_id else None
    if previous == str(release.id):
        return
    await db.execute(update(Artifact).where(Artifact.id == str(artifact.id))
                     .values(published_release_id=str(release.id)))
    await db.commit()
    await _audit(db, artifact, user, "dashboard.rolled_back", {"release_id": str(release.id), "from": previous})


async def transfer(db, artifact: Artifact, actor, organization, new_owner_id: str) -> None:
    """The dashboard to another editor; the previous owner stays an editor."""
    from app.models.artifact_release import ArtifactShare
    from app.models.user import User
    from app.services.dashboard_access import can_edit

    target = await db.get(User, str(new_owner_id))
    if target is None or not await can_edit(db, artifact, target):
        raise AppError.bad_request(ErrorCode.DASHBOARD_TRANSFER_NOT_EDITOR,
                                   "Ownership can go only to someone who can edit the dashboard")
    previous = str(artifact.owner_id) if artifact.owner_id else None
    if previous == str(target.id):
        return
    await db.execute(update(Artifact).where(Artifact.id == str(artifact.id)).values(owner_id=str(target.id)))
    from sqlalchemy import delete
    await db.execute(delete(ArtifactShare).where(ArtifactShare.artifact_id == str(artifact.id),
                                                 ArtifactShare.user_id == str(target.id)))
    if previous:
        await db.execute(delete(ArtifactShare).where(ArtifactShare.artifact_id == str(artifact.id),
                                                     ArtifactShare.user_id == previous))
        db.add(ArtifactShare(artifact_id=str(artifact.id), user_id=previous, role="edit"))
    await db.commit()
    await _audit(db, artifact, actor, "dashboard.ownership_transferred", {"from": previous, "to": str(target.id)})


async def _audit(db, artifact, user, action, details) -> None:
    try:
        from app.ee.audit.service import audit_service

        await audit_service.log(db=db, organization_id=str(artifact.organization_id), action=action,
                                user_id=str(user.id) if user is not None else None, resource_type="artifact",
                                resource_id=str(artifact.id), details={"title": artifact.title, **details})
    except Exception:
        logger.warning("dashboard audit log failed", exc_info=True)
