"""Sites an admin allows to show BOW in an iframe (Settings → General).

Instance-wide, like the license: the sign-in page belongs to no organization,
so one list serves the whole deployment. It lives in ``instance_settings``
(key ``frame_ancestors``); core/security_headers.py adds it to the default
``frame-ancestors`` list.

The security-headers middleware runs on every request, /health included, so
it must never wait on the database. ``cached_origins`` answers from memory at
once; when the list is older than ``REFRESH_SECONDS`` it starts one background
read (bounded by ``READ_TIMEOUT_SECONDS``) and the next requests get the
result. A failed or slow read keeps the previous list. Each worker reads the
list once at startup (``warm``), so a restart does not serve the bare default
to the first framed page. A save refreshes this process at once, and a
background read that started before the save cannot overwrite it; other
workers and replicas follow within the window.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Iterable, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.security_headers import frame_ancestor_source
from app.errors import AppError, ErrorCode
from app.models.instance_settings import InstanceSettings

logger = logging.getLogger(__name__)

SETTING_KEY = "frame_ancestors"
REFRESH_SECONDS = 30
READ_TIMEOUT_SECONDS = 2
MAX_ORIGINS = 50

# "task" keeps a reference to the running refresh so it is not collected.
# "generation" changes on every save or reset; a background read that started
# under an older generation drops its result instead of overwriting newer data.
_cache: dict = {"origins": (), "loaded_at": None, "task": None, "generation": 0}


def normalize(origins: Iterable[str]) -> List[str]:
    """Validated, deduplicated origins in the order given; raises a typed
    400 naming every entry that is not a plain https origin."""
    result: List[str] = []
    invalid: List[str] = []
    for value in origins:
        if isinstance(value, str) and not value.strip():
            continue
        source = frame_ancestor_source(value)
        if source is None:
            invalid.append(str(value))
        elif source not in result:
            result.append(source)
    if invalid:
        shown = ", ".join(v if len(v) <= 100 else v[:100] + "…" for v in invalid[:10])
        raise AppError.bad_request(
            ErrorCode.FRAME_ANCESTORS_INVALID,
            f"Not a valid site address: {shown}",
            origins=shown,
        )
    if len(result) > MAX_ORIGINS:
        raise AppError.bad_request(
            ErrorCode.FRAME_ANCESTORS_TOO_MANY,
            f"At most {MAX_ORIGINS} sites can be listed",
            max=MAX_ORIGINS,
        )
    return result


async def _row(db: AsyncSession, *, include_deleted: bool = False) -> Optional[InstanceSettings]:
    query = select(InstanceSettings).where(InstanceSettings.key == SETTING_KEY)
    if not include_deleted:
        query = query.where(InstanceSettings.deleted_at.is_(None))
    return (await db.execute(query)).scalar_one_or_none()


def _stored_origins(row: Optional[InstanceSettings]) -> List[str]:
    # Re-check on read: a row edited by hand must not reach the header, and a
    # malformed one reads as empty instead of failing the page.
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    origins = value.get("origins")
    if not isinstance(origins, list):
        return []
    clean: List[str] = []
    for source in (frame_ancestor_source(o) for o in origins):
        if source and source not in clean:
            clean.append(source)
    return clean[:MAX_ORIGINS]


async def get_origins(db: AsyncSession) -> List[str]:
    return _stored_origins(await _row(db))


async def save_origins(db: AsyncSession, origins: Iterable[str], *, user_id: str) -> List[str]:
    clean = normalize(origins)
    value = {"origins": clean, "updated_by": str(user_id), "updated_at": datetime.utcnow().isoformat()}
    try:
        await _write(db, value)
    except IntegrityError:
        # Two first saves at once: the other one inserted the row; update it.
        await db.rollback()
        await _write(db, value)
    _set_saved(tuple(clean))
    return clean


async def _write(db: AsyncSession, value: dict) -> None:
    # A soft-deleted row still holds the unique key, so revive it rather
    # than insert next to it.
    row = await _row(db, include_deleted=True)
    if row is None:
        db.add(InstanceSettings(key=SETTING_KEY, value=value))
    else:
        row.value = value
        row.deleted_at = None
        row.updated_at = datetime.utcnow()
        flag_modified(row, "value")
        db.add(row)
    await db.commit()


def _set_saved(origins: Tuple[str, ...]) -> None:
    _cache["generation"] += 1
    _cache.update(origins=origins, loaded_at=time.monotonic())


def cached_origins() -> Tuple[str, ...]:
    """The saved list for the middleware, from memory and without waiting.
    Starts a background re-read when it is stale and none is running."""
    loaded_at = _cache["loaded_at"]
    stale = loaded_at is None or time.monotonic() - loaded_at >= REFRESH_SECONDS
    task = _cache["task"]
    if stale and (task is None or task.done()):
        try:
            _cache["task"] = asyncio.get_running_loop().create_task(_refresh())
        except RuntimeError:
            pass  # no running loop: nothing to schedule on
    return _cache["origins"]


async def _read() -> Tuple[str, ...]:
    from app.dependencies import async_session_maker

    async with async_session_maker() as db:
        return tuple(await get_origins(db))


async def _refresh() -> None:
    generation = _cache["generation"]
    try:
        origins = await asyncio.wait_for(_read(), READ_TIMEOUT_SECONDS)
        if _cache["generation"] == generation:
            _cache["origins"] = origins
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Could not read the allowed framing sites; keeping the previous list", exc_info=True)
    finally:
        # A save or reset during the read owns loaded_at now.
        if _cache["generation"] == generation:
            _cache["loaded_at"] = time.monotonic()


async def warm() -> None:
    """Read the list once at startup (bounded), so the first framed page after
    a restart already gets the saved sites."""
    await _refresh()


def reset_cache() -> None:
    """Forget the cached list (tests, and after the database is replaced)."""
    task = _cache["task"]
    if task is not None and not task.done():
        task.cancel()
    _cache["generation"] += 1
    _cache.update(origins=(), loaded_at=None, task=None)
