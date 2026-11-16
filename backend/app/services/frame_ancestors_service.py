"""Sites an admin allows to show BOW in an iframe (Settings → General).

Instance-wide, like the license: the sign-in page belongs to no organization,
so one list serves the whole deployment. It lives in ``instance_settings``
(key ``frame_ancestors``) and adds to ``security.frame_ancestors`` from the
config file; core/security_headers.py puts both into ``frame-ancestors``.

The security-headers middleware runs on every request, /health included, so
it must never wait on the database. ``cached_origins`` answers from memory at
once; when the list is older than ``REFRESH_SECONDS`` it starts one background
read (bounded by ``READ_TIMEOUT_SECONDS``) and the next requests get the
result. A failed or slow read keeps the previous list; before the first read
lands only the default policy applies (the stricter side). A save refreshes
this process at once; other workers and replicas follow within the window.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Iterable, List, Optional, Tuple

from sqlalchemy import select
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
_cache: dict = {"origins": (), "loaded_at": None, "task": None}


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
        raise AppError.bad_request(
            ErrorCode.FRAME_ANCESTORS_INVALID,
            f"Not a valid site address: {', '.join(invalid)}",
            origins=", ".join(invalid),
        )
    if len(result) > MAX_ORIGINS:
        raise AppError.bad_request(
            ErrorCode.FRAME_ANCESTORS_TOO_MANY,
            f"At most {MAX_ORIGINS} sites can be listed",
            max=MAX_ORIGINS,
        )
    return result


async def _row(db: AsyncSession) -> Optional[InstanceSettings]:
    result = await db.execute(
        select(InstanceSettings).where(
            InstanceSettings.key == SETTING_KEY,
            InstanceSettings.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


def _stored_origins(row: Optional[InstanceSettings]) -> List[str]:
    origins = (row.value or {}).get("origins") if row is not None else None
    # Re-check on read: a row edited by hand must not reach the header.
    return [s for s in (frame_ancestor_source(o) for o in origins or ()) if s]


async def get_origins(db: AsyncSession) -> List[str]:
    return _stored_origins(await _row(db))


async def save_origins(db: AsyncSession, origins: Iterable[str], *, user_id: str) -> List[str]:
    clean = normalize(origins)
    row = await _row(db)
    value = {"origins": clean, "updated_by": str(user_id), "updated_at": datetime.utcnow().isoformat()}
    if row is None:
        db.add(InstanceSettings(key=SETTING_KEY, value=value))
    else:
        row.value = value
        row.updated_at = datetime.utcnow()
        flag_modified(row, "value")
        db.add(row)
    await db.commit()
    _cache.update(origins=tuple(clean), loaded_at=time.monotonic())
    return clean


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
    try:
        _cache["origins"] = await asyncio.wait_for(_read(), READ_TIMEOUT_SECONDS)
    except Exception:
        logger.warning("Could not read the allowed framing sites; keeping the previous list", exc_info=True)
    finally:
        _cache["loaded_at"] = time.monotonic()


def reset_cache() -> None:
    """Forget the cached list (tests, and after the database is replaced)."""
    task = _cache["task"]
    if task is not None and not task.done():
        task.cancel()
    _cache.update(origins=(), loaded_at=None, task=None)
