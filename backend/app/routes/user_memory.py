"""Self-service user memory API — the current user's own entries only.

There is deliberately no admin endpoint: memory is private to its owner.
Every route is scoped to (current user, current organization), so another
member or an org admin simply cannot address someone else's entries (404).
All routes return 403 ``memory.disabled`` when the org turned
``enable_user_memory`` off; stored entries are kept and come back when it is
turned on again.
"""
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.context.builders.memory_context_builder import build_memory_context
from app.core.auth import current_user
from app.dependencies import get_async_db, get_current_organization
from app.errors import AppError, ErrorCode
from app.models.memory_entry import MemoryEntry
from app.models.organization import Organization
from app.models.organization_settings import OrganizationSettings
from app.models.report import Report
from app.models.user import User
from app.services import memory_rules as R
from app.services.memory_rules import MemoryValidationError
from app.services.memory_service import is_memory_enabled, memory_service

router = APIRouter(tags=["users"])


class MemoryEvidenceSchema(BaseModel):
    report_id: Optional[str] = None
    report_title: Optional[str] = None
    report_link: Optional[str] = None
    quote: Optional[str] = None


class MemoryEntrySchema(BaseModel):
    id: str
    handle: str
    section: str
    text: str
    tags: List[str] = []
    aliases: List[str] = []
    event_start: Optional[datetime] = None
    event_end: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    effective_expires_at: Optional[datetime] = None
    expired: bool = False
    source: str
    evidence: Optional[MemoryEvidenceSchema] = None
    seen_count: int = 1
    last_seen_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class MemoryTagCount(BaseModel):
    tag: str
    count: int


class MemoryListResponse(BaseModel):
    enabled: bool = True
    sections: Dict[str, List[MemoryEntrySchema]]
    total: int
    cap: int = R.MAX_ACTIVE_ENTRIES
    tags: List[MemoryTagCount] = []
    # What the agent sees without any topic match: the always tier + the
    # index line, rendered exactly as it is injected.
    preview: str = ""
    preview_chars: int = 0


class MemoryCreateRequest(BaseModel):
    text: str = Field(..., max_length=2000)
    section: str
    tags: Optional[List[str]] = None
    aliases: Optional[List[str]] = None
    event_start: Optional[str] = None
    event_end: Optional[str] = None
    expires_at: Optional[str] = None


class MemoryUpdateRequest(BaseModel):
    text: Optional[str] = Field(default=None, max_length=2000)
    section: Optional[str] = None
    tags: Optional[List[str]] = None
    aliases: Optional[List[str]] = None
    event_start: Optional[str] = None
    event_end: Optional[str] = None
    expires_at: Optional[str] = None


class ForgetAllResponse(BaseModel):
    forgotten: int


async def _require_enabled(db: AsyncSession, organization: Organization) -> None:
    row = await db.execute(
        select(OrganizationSettings).where(OrganizationSettings.organization_id == organization.id)
    )
    settings = row.scalar_one_or_none()
    if not is_memory_enabled(settings):
        raise AppError.forbidden(ErrorCode.MEMORY_DISABLED, "User memory is turned off for this organization.")


def _raise_validation(e: MemoryValidationError):
    raise AppError.bad_request(e.code, e.message)


async def _serialize(
    db: AsyncSession, organization: Organization, entries: List[MemoryEntry], now: datetime
) -> List[MemoryEntrySchema]:
    report_ids = {
        (e.evidence or {}).get("report_id") for e in entries if isinstance(e.evidence, dict)
    } - {None}
    titles: Dict[str, str] = {}
    if report_ids:
        rows = await db.execute(
            select(Report.id, Report.title).where(
                Report.id.in_(list(report_ids)), Report.organization_id == str(organization.id)
            )
        )
        titles = {str(r[0]): (r[1] or "") for r in rows.all()}
    out: List[MemoryEntrySchema] = []
    for e in entries:
        ev = e.evidence if isinstance(e.evidence, dict) else None
        evidence = None
        if ev:
            rid = ev.get("report_id")
            evidence = MemoryEvidenceSchema(
                report_id=rid,
                # Only link reports that still exist in this org.
                report_title=titles.get(rid) if rid in titles else None,
                report_link=f"/reports/{rid}" if rid in titles else None,
                quote=ev.get("quote"),
            )
        eff = R.effective_expiry(
            section=e.section, expires_at=e.expires_at, event_start=e.event_start,
            event_end=e.event_end, last_seen_at=e.last_seen_at,
        )
        out.append(MemoryEntrySchema(
            id=str(e.id), handle=e.handle, section=e.section, text=e.text or "",
            tags=list(e.tags or []), aliases=list(e.aliases or []),
            event_start=e.event_start, event_end=e.event_end, expires_at=e.expires_at,
            effective_expires_at=eff, expired=bool(eff is not None and now >= eff),
            source=e.source, evidence=evidence, seen_count=int(e.seen_count or 1),
            last_seen_at=e.last_seen_at, created_at=e.created_at, updated_at=e.updated_at,
        ))
    return out


async def _get_own_entry(db, organization, user, entry_id) -> MemoryEntry:
    entry = await memory_service.get_entry(db, str(organization.id), str(user.id), entry_id)
    if entry is None or entry.status != "active":
        raise AppError.not_found(ErrorCode.MEMORY_NOT_FOUND, "Memory entry not found.")
    return entry


@router.get("/users/me/memory", response_model=MemoryListResponse)
async def list_my_memory(
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    await _require_enabled(db, organization)
    now = datetime.utcnow()
    entries = await memory_service.active_entries(db, str(organization.id), str(user.id), now=now, include_expired=True)
    serialized = await _serialize(db, organization, entries, now)
    sections: Dict[str, List[MemoryEntrySchema]] = {s: [] for s in R.SECTIONS}
    for item in serialized:
        sections.setdefault(item.section, []).append(item)
    sections["events"].sort(key=lambda x: x.event_start or datetime.max)

    from collections import Counter
    counts = Counter(t for e in entries if not R.is_expired(e, now) for t in (e.tags or []) if not R.is_object_tag(t))
    ctx = build_memory_context(entries, now=now, user_name=getattr(user, "name", None))
    return MemoryListResponse(
        sections=sections,
        total=sum(1 for i in serialized if not i.expired),
        tags=[MemoryTagCount(tag=t, count=c) for t, c in counts.most_common()],
        preview=ctx.body,
        preview_chars=ctx.chars,
    )


@router.post("/users/me/memory", response_model=MemoryEntrySchema)
async def create_my_memory(
    payload: MemoryCreateRequest,
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    await _require_enabled(db, organization)
    try:
        result = await memory_service.create(
            db,
            organization_id=str(organization.id),
            user_id=str(user.id),
            text=payload.text,
            section=payload.section,
            tags=payload.tags,
            aliases=payload.aliases,
            event_start=payload.event_start,
            event_end=payload.event_end,
            expires_at=payload.expires_at,
            source="user",
        )
    except MemoryValidationError as e:
        _raise_validation(e)
    return (await _serialize(db, organization, [result.entry], datetime.utcnow()))[0]


@router.patch("/users/me/memory/{entry_id}", response_model=MemoryEntrySchema)
async def update_my_memory(
    entry_id: str,
    payload: MemoryUpdateRequest,
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    await _require_enabled(db, organization)
    entry = await _get_own_entry(db, organization, user, entry_id)
    changes = payload.model_dump(exclude_unset=True)
    # Empty strings clear optional dates.
    for k in ("event_end", "expires_at"):
        if k in changes and changes[k] in ("", None):
            changes[k] = None
    try:
        new = await memory_service.update(db, entry, changes=changes, source="user")
    except MemoryValidationError as e:
        _raise_validation(e)
    return (await _serialize(db, organization, [new], datetime.utcnow()))[0]


@router.delete("/users/me/memory/{entry_id}", response_model=MemoryEntrySchema)
async def forget_my_memory(
    entry_id: str,
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    await _require_enabled(db, organization)
    entry = await _get_own_entry(db, organization, user, entry_id)
    await memory_service.forget(db, entry)
    return (await _serialize(db, organization, [entry], datetime.utcnow()))[0]


@router.delete("/users/me/memory", response_model=ForgetAllResponse)
async def forget_all_my_memory(
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    await _require_enabled(db, organization)
    n = await memory_service.forget_all(db, str(organization.id), str(user.id))
    return ForgetAllResponse(forgotten=n)
