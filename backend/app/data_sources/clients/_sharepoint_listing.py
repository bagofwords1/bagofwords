"""Bounded interactive listing, opted into by SharePoint clients only."""
import asyncio
import fnmatch
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone


@dataclass
class SharepointListingResult:
    files: list[dict]
    stop_reason: str | None = None


def parse_time_bound(value, field):
    """An ISO-8601 bound as an aware UTC datetime, or None when unset.

    Date-only values mean UTC midnight; naive datetimes are taken as UTC.
    """
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{field} must be an ISO-8601 date or datetime (for example 2026-01-31 or 2026-01-31T09:00:00Z).")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _entry_time(value):
    """A file's source timestamp as an aware UTC datetime, or None if absent/unparseable."""
    if not value:
        return None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    try:
        return parse_time_bound(value, "timestamp")
    except ValueError:
        return None


def time_bounds(**bounds):
    """Validate created/modified after/before bounds → {(key, op): datetime}.

    Lower bounds are inclusive, upper bounds exclusive. A reversed range is
    rejected rather than silently matching nothing.
    """
    parsed = {}
    for key in ("created", "modified"):
        after = parse_time_bound(bounds.get(f"{key}_after"), f"{key}_after")
        before = parse_time_bound(bounds.get(f"{key}_before"), f"{key}_before")
        if after is not None and before is not None and after >= before:
            raise ValueError(f"{key}_after must be earlier than {key}_before.")
        if after is not None:
            parsed[(f"{key}_at", "after")] = after
        if before is not None:
            parsed[(f"{key}_at", "before")] = before
    return parsed


def matches_time_bounds(row, bounds):
    """A file without the bounded timestamp never matches that bound."""
    for (field, op), limit in bounds.items():
        stamp = _entry_time(row.get(field))
        if stamp is None or (stamp < limit if op == "after" else stamp >= limit):
            return False
    return True


class SharepointBoundedListing:
    """Keep interactive limits separate from full indexing and other connectors."""

    async def alist_files_bounded(self, *, folder_id=None, recursive=False, limit=50, name_pattern=None,
                                  created_after=None, created_before=None,
                                  modified_after=None, modified_before=None):
        return await asyncio.to_thread(
            self.list_files_bounded, folder_id=folder_id, recursive=recursive,
            limit=limit, name_pattern=name_pattern,
            created_after=created_after, created_before=created_before,
            modified_after=modified_after, modified_before=modified_before,
        )

    def list_files_bounded(self, *, folder_id=None, recursive=False, limit=50, name_pattern=None,
                           created_after=None, created_before=None,
                           modified_after=None, modified_before=None):
        if not 1 <= limit <= 500:
            raise ValueError('SharePoint listing limit must be between 1 and 500.')
        bounds = time_bounds(created_after=created_after, created_before=created_before,
                             modified_after=modified_after, modified_before=modified_before)
        files, seen = [], set()
        pattern = (name_pattern or '').lower()
        # Page size bounds each response; limit bounds matching results across pages.
        # Do not fetch an extra page merely to establish whether more files exist.
        with closing(self._iter_listing_files(folder_id, recursive, min(limit, 200))) as rows:
            for row in rows:
                if row['id'] in seen:
                    continue
                seen.add(row['id'])
                if (not pattern or any(fnmatch.fnmatch(str(row.get(key) or '').lower(), pattern)
                                       for key in ('name', 'path'))) and matches_time_bounds(row, bounds):
                    files.append(row)
                if len(files) >= limit:
                    return SharepointListingResult(files, 'result_limit')
                if len(seen) >= self.max_catalog_objects:
                    return SharepointListingResult(files, 'catalog_limit')
        return SharepointListingResult(files)
