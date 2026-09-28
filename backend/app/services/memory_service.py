"""MemoryService — the one write/read path for user memory entries.

Every writer goes through here: the agent tools (create_memory / edit_memory)
and the self-service user API. That is what keeps the invariants in one place:

- dedupe on write (the same normalized text strengthens the existing entry;
  a shared alias of the user's shorthand merges too),
- edits update the row in place (the handle stays the same),
- forget blanks content (only id/status/timestamps survive),
- computed expiry on read (no background job),
- a per-user cap with eviction of the weakest non-user entry,
- facts only: text that reads like a rule about how to answer or compute is
  refused on every write (it belongs in instructions), plus the secret
  filter on every write.

Pure rules live in ``memory_rules`` so they can be tested without a DB.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.memory_entry import MemoryEntry
from app.services import memory_rules as R
from app.services.memory_rules import MemoryValidationError

logger = logging.getLogger(__name__)


@dataclass
class WriteResult:
    entry: MemoryEntry
    deduped: bool = False
    evicted: List[str] = field(default_factory=list)  # handles evicted by the cap


@dataclass
class CleanPayload:
    text: str
    tags: List[str]
    aliases: List[str]
    event_start: Optional[datetime]
    event_end: Optional[datetime]
    expires_at: Optional[datetime]


def _utcnow() -> datetime:
    return datetime.utcnow()


def is_memory_enabled(organization_settings) -> bool:
    """The ``enable_user_memory`` org setting (defaults on)."""
    if organization_settings is None:
        return True
    try:
        cfg = organization_settings.get_config("enable_user_memory")
    except Exception:
        return True
    if cfg is None:
        return True
    return bool(getattr(cfg, "value", True))


class MemoryService:
    # ------------------------------------------------------------------ reads

    async def list_entries(
        self,
        db: AsyncSession,
        organization_id: str,
        user_id: str,
        *,
        statuses: Sequence[str] = ("active",),
    ) -> List[MemoryEntry]:
        rows = await db.execute(
            select(MemoryEntry)
            .where(
                MemoryEntry.organization_id == str(organization_id),
                MemoryEntry.user_id == str(user_id),
                MemoryEntry.status.in_(list(statuses)),
            )
            .order_by(MemoryEntry.seq.asc())
        )
        return list(rows.scalars().all())

    async def active_entries(
        self,
        db: AsyncSession,
        organization_id: str,
        user_id: str,
        *,
        now: Optional[datetime] = None,
        include_expired: bool = False,
    ) -> List[MemoryEntry]:
        now = now or _utcnow()
        entries = await self.list_entries(db, organization_id, user_id)
        if include_expired:
            return entries
        return [e for e in entries if not R.is_expired(e, now)]

    async def get_entry(
        self, db: AsyncSession, organization_id: str, user_id: str, entry_id: str
    ) -> Optional[MemoryEntry]:
        row = await db.execute(
            select(MemoryEntry).where(
                MemoryEntry.id == str(entry_id),
                MemoryEntry.organization_id == str(organization_id),
                MemoryEntry.user_id == str(user_id),
            )
        )
        return row.scalar_one_or_none()

    async def resolve_handle(
        self, db: AsyncSession, organization_id: str, user_id: str, handle: str
    ) -> Optional[MemoryEntry]:
        """The active entry a handle ("m7") refers to, or None."""
        h = (handle or "").strip().lower().lstrip("[").rstrip("]")
        if not h:
            return None
        row = await db.execute(
            select(MemoryEntry).where(
                MemoryEntry.organization_id == str(organization_id),
                MemoryEntry.user_id == str(user_id),
                MemoryEntry.handle == h,
                MemoryEntry.status == "active",
            )
        )
        return row.scalar_one_or_none()

    # ------------------------------------------------------------ validation

    def clean_payload(
        self,
        *,
        text: Optional[str],
        tags: Optional[Iterable[str]] = None,
        aliases: Optional[Iterable[str]] = None,
        event_start=None,
        event_end=None,
        expires_at=None,
        require_tags: bool = False,
        agent_write: bool = False,
    ) -> CleanPayload:
        body = R.clean_text(text or "")
        if not body:
            raise MemoryValidationError("memory.text_required", "Memory text is empty.")
        if len(body) > R.MAX_TEXT_CHARS:
            raise MemoryValidationError(
                "memory.text_too_long",
                f"Memory text is {len(body)} chars; the limit is {R.MAX_TEXT_CHARS}. "
                "Keep it to one short declarative fact.",
            )

        raw_tags = list(tags or [])
        if len(raw_tags) > R.MAX_TAGS:
            raise MemoryValidationError(
                "memory.too_many_tags", f"Use at most {R.MAX_TAGS} tags (got {len(raw_tags)})."
            )
        norm_tags = R.normalize_tags(raw_tags)
        if require_tags and not norm_tags:
            raise MemoryValidationError(
                "memory.tags_required",
                "Give 1-4 short tags (lowercase slugs like 'emea', 'board-deck'); reuse tags already "
                "shown in the <memory> index.",
            )
        norm_aliases = R.normalize_aliases(aliases)

        start = R.parse_when(event_start)
        end = R.parse_when(event_end)
        exp = R.parse_when(expires_at)
        if end is not None and start is None:
            start, end = end, None
        if start is not None and end is not None and end < start:
            raise MemoryValidationError("memory.invalid_date", "event_end is before event_start.")

        if R.contains_secret(body) or any(R.contains_secret(a) for a in norm_aliases):
            raise MemoryValidationError(
                "memory.sensitive",
                "That looks like a credential or secret. Secrets are never stored in memory.",
            )
        if agent_write and (R.contains_sensitive(body) or any(R.contains_sensitive(a) for a in norm_aliases)):
            raise MemoryValidationError(
                "memory.sensitive",
                "Memory only holds work context. Don't store health or other personal details "
                "(for time off, record only the dates of availability).",
            )
        hit = R.looks_like_rule(body)
        if hit:
            raise MemoryValidationError(
                "memory.looks_like_rule",
                (
                    f"This is a rule about how to answer or compute (matched: \"{hit}\"), not a fact about "
                    "the user. Rules are never memory: apply it for the rest of this conversation, starting "
                    "with the current answer. Lasting rules live in instructions, which people write. "
                    "Memory is only for facts: their work, projects, dates, what they follow, their own "
                    "shorthand."
                ) if agent_write else (
                    "That reads like a rule for how to answer. Rules go in your Custom instructions; "
                    "memory keeps facts about you."
                ),
            )
        return CleanPayload(
            text=body, tags=norm_tags, aliases=norm_aliases,
            event_start=start, event_end=end, expires_at=exp,
        )

    # ----------------------------------------------------------------- writes

    async def _next_seq(self, db: AsyncSession, organization_id: str, user_id: str) -> int:
        row = await db.execute(
            select(func.max(MemoryEntry.seq)).where(
                MemoryEntry.organization_id == str(organization_id),
                MemoryEntry.user_id == str(user_id),
            )
        )
        return int(row.scalar() or 0) + 1

    async def _insert(self, db: AsyncSession, organization_id: str, user_id: str, **fields) -> MemoryEntry:
        """Insert with a fresh per-user handle. Concurrent writers can race on
        the same ``seq``; the unique constraint catches it and we retry with
        the next number inside a savepoint, so no write is lost."""
        last_exc: Optional[Exception] = None
        for _ in range(8):
            seq = await self._next_seq(db, organization_id, user_id)
            entry = MemoryEntry(
                organization_id=str(organization_id),
                user_id=str(user_id),
                seq=seq,
                handle=f"m{seq}",
                **fields,
            )
            try:
                async with db.begin_nested():
                    db.add(entry)
                    await db.flush()
                return entry
            except IntegrityError as exc:  # seq taken by a concurrent writer
                last_exc = exc
                continue
        raise last_exc or RuntimeError("could not allocate a memory handle")

    async def _find_duplicate(
        self, db: AsyncSession, organization_id: str, user_id: str, payload: CleanPayload
    ) -> Optional[MemoryEntry]:
        key = R.normalize_text(payload.text)
        new_alias_keys = {R.normalize_text(a) for a in payload.aliases}
        for e in await self.list_entries(db, organization_id, user_id):
            if R.normalize_text(e.text) == key:
                return e
            if new_alias_keys:
                existing = {R.normalize_text(a) for a in (e.aliases or [])}
                if existing & new_alias_keys:
                    return e
        return None

    async def create(
        self,
        db: AsyncSession,
        *,
        organization_id: str,
        user_id: str,
        text: Optional[str],
        tags: Optional[Iterable[str]] = None,
        aliases: Optional[Iterable[str]] = None,
        event_start=None,
        event_end=None,
        expires_at=None,
        source: str = "agent",
        evidence: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
        commit: bool = True,
    ) -> WriteResult:
        now = now or _utcnow()
        agent_write = source == "agent"
        payload = self.clean_payload(
            text=text, tags=tags, aliases=aliases,
            event_start=event_start, event_end=event_end, expires_at=expires_at,
            require_tags=agent_write, agent_write=agent_write,
        )

        dup = await self._find_duplicate(db, organization_id, user_id, payload)
        if dup is not None:
            dup.seen_count = int(dup.seen_count or 1) + 1
            dup.last_seen_at = now
            merged_aliases = R.normalize_aliases(list(dup.aliases or []) + payload.aliases)
            dup.aliases = merged_aliases or None
            merged_tags = R.normalize_tags(list(dup.tags or []) + payload.tags)[: R.MAX_TAGS]
            dup.tags = merged_tags
            if payload.event_start is not None:
                dup.event_start = dup.event_start or payload.event_start
                dup.event_end = dup.event_end or payload.event_end
            db.add(dup)
            if commit:
                await db.commit()
                await db.refresh(dup)
            return WriteResult(entry=dup, deduped=True)

        await self._ensure_capacity(db, organization_id, user_id, source=source, now=now)
        entry = await self._insert(
            db, organization_id, user_id,
            text=payload.text,
            tags=payload.tags,
            aliases=payload.aliases or None,
            event_start=payload.event_start,
            event_end=payload.event_end,
            expires_at=payload.expires_at,
            source=source,
            evidence=evidence or None,
            seen_count=1,
            last_seen_at=now,
            status="active",
        )
        evicted = await self._evict_over_cap(db, organization_id, user_id, now=now, keep_id=str(entry.id))
        if commit:
            await db.commit()
            await db.refresh(entry)
        return WriteResult(entry=entry, deduped=False, evicted=evicted)

    async def update(
        self,
        db: AsyncSession,
        entry: MemoryEntry,
        *,
        changes: Dict[str, Any],
        source: str,
        evidence: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> MemoryEntry:
        """Apply ``changes`` to ``entry`` in place (same row, same handle)."""
        if entry.status != "active":
            raise MemoryValidationError("memory.not_active", "That memory entry is no longer active.")
        now = now or _utcnow()
        agent_write = source == "agent"

        def pick(key, current):
            return changes[key] if key in changes and changes[key] is not None else current

        payload = self.clean_payload(
            text=pick("text", entry.text),
            tags=pick("tags", entry.tags or []),
            aliases=pick("aliases", entry.aliases or []),
            event_start=changes["event_start"] if "event_start" in changes else entry.event_start,
            event_end=changes["event_end"] if "event_end" in changes else entry.event_end,
            expires_at=changes["expires_at"] if "expires_at" in changes else entry.expires_at,
            require_tags=False,
            agent_write=agent_write,
        )
        entry.text = payload.text
        entry.tags = payload.tags
        entry.aliases = payload.aliases or None
        entry.event_start = payload.event_start
        entry.event_end = payload.event_end
        entry.expires_at = payload.expires_at
        # A user edit makes the entry theirs; an agent edit of a user-authored
        # entry (only on a direct request) keeps it theirs.
        if source == "user":
            entry.source = "user"
        if evidence:
            entry.evidence = evidence
        entry.last_seen_at = now
        db.add(entry)
        await db.commit()
        await db.refresh(entry)
        return entry

    @staticmethod
    def _blank(entry: MemoryEntry) -> None:
        entry.status = "forgotten"
        entry.text = ""
        entry.aliases = None
        entry.tags = []
        entry.evidence = None

    async def forget(self, db: AsyncSession, entry: MemoryEntry, *, commit: bool = True) -> MemoryEntry:
        self._blank(entry)
        db.add(entry)
        if commit:
            await db.commit()
        return entry

    async def forget_all(self, db: AsyncSession, organization_id: str, user_id: str) -> int:
        """Forget every active entry. Returns how many were forgotten."""
        entries = await self.list_entries(db, organization_id, user_id)
        for e in entries:
            self._blank(e)
            db.add(e)
        await db.commit()
        return len(entries)

    async def delete_for_membership(self, db: AsyncSession, organization_id: str, user_id: str) -> None:
        """Hard-delete a user's entries in an org (membership removed). No
        commit — runs inside the caller's removal transaction."""
        from sqlalchemy import delete
        await db.execute(
            delete(MemoryEntry).where(
                MemoryEntry.organization_id == str(organization_id),
                MemoryEntry.user_id == str(user_id),
            )
        )

    async def touch(self, db: AsyncSession, entries: Sequence[MemoryEntry], now: Optional[datetime] = None) -> None:
        """Mark entries as seen again (e.g. an agent re-confirmed them)."""
        now = now or _utcnow()
        for e in entries:
            e.seen_count = int(e.seen_count or 1) + 1
            e.last_seen_at = now
            db.add(e)
        await db.commit()

    # -------------------------------------------------------------- the cap

    @staticmethod
    def _eviction_order(entries: Sequence[MemoryEntry]) -> List[MemoryEntry]:
        return sorted(
            (e for e in entries if e.source != "user"),
            key=lambda e: (int(e.seen_count or 0), e.last_seen_at or datetime.min, e.seq or 0),
        )

    async def _ensure_capacity(self, db, organization_id, user_id, *, source: str, now: datetime) -> None:
        live = await self.active_entries(db, organization_id, user_id, now=now)
        if len(live) < R.MAX_ACTIVE_ENTRIES:
            return
        if not self._eviction_order(live):
            raise MemoryValidationError(
                "memory.full",
                f"Memory is full ({R.MAX_ACTIVE_ENTRIES} entries you wrote yourself). Delete some in "
                "your profile first.",
            )

    async def _evict_over_cap(self, db, organization_id, user_id, *, now: datetime, keep_id: str) -> List[str]:
        live = await self.active_entries(db, organization_id, user_id, now=now)
        over = len(live) - R.MAX_ACTIVE_ENTRIES
        evicted: List[str] = []
        if over <= 0:
            return evicted
        for e in self._eviction_order(live):
            if over <= 0:
                break
            if str(e.id) == keep_id:
                continue
            self._blank(e)
            db.add(e)
            evicted.append(e.handle)
            over -= 1
        if evicted:
            logger.info("memory cap: evicted %s for user %s", evicted, user_id)
        return evicted

    # --------------------------------------------------------------- search

    async def search(
        self,
        db: AsyncSession,
        organization_id: str,
        user_id: str,
        *,
        query: Optional[str] = None,
        tags: Optional[Iterable[str]] = None,
        include_past_events: bool = False,
        limit: int = 10,
        exclude_ids: Optional[Iterable[str]] = None,
        now: Optional[datetime] = None,
    ) -> List[MemoryEntry]:
        from app.ai.context.builders.memory_context_builder import score_entry
        from app.ai.context.keyword_match import extract_keywords

        now = now or _utcnow()
        limit = max(1, min(int(limit or 10), 25))
        excluded = {str(x) for x in (exclude_ids or [])}
        want_tags = set(R.normalize_tags(tags))
        keywords = extract_keywords(query or "", unicode=True)

        pool = await self.list_entries(db, organization_id, user_id)
        scored: List[tuple[float, MemoryEntry]] = []
        for e in pool:
            if str(e.id) in excluded:
                continue
            if R.is_expired(e, now) and not (include_past_events and R.is_dated(e)):
                continue
            if want_tags and not (want_tags & set(e.tags or [])):
                continue
            score = 1.0
            if keywords:
                score, _hits = score_entry(e, keywords, set())
                if score <= 0:
                    continue
            scored.append((score, e))
        scored.sort(key=lambda p: p[1].last_seen_at or datetime.min, reverse=True)
        scored.sort(key=lambda p: p[0], reverse=True)  # stable: ties keep most-recent first
        return [e for _, e in scored[:limit]]


memory_service = MemoryService()
