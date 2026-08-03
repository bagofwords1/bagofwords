"""Persistence for the instance's enterprise license key.

The key set in Settings → License lives in ``instance_settings`` (one row, key
``license``) and overrides the bow-config / BOW_LICENSE_KEY value. Because
``app.ee.license`` is synchronous, this module is the only place that reads the
row and mirrors it into that module via ``set_db_license_key``.

Mirroring happens at three moments:

* application startup, before the first request is served;
* immediately after a save/remove, so the worker that handled the write is
  correct straight away;
* every ``REFRESH_INTERVAL_SECONDS`` in *every* worker, so the other uvicorn
  workers and other pods converge without a restart.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.ee.license import (
    LicenseInfo,
    get_db_license_key,
    get_license_info,
    set_db_license_key,
    validate_license_key,
)
from app.models.instance_settings import InstanceSettings

logger = logging.getLogger(__name__)

LICENSE_SETTING_KEY = "license"

# How often each worker re-reads the stored key. The license changes roughly
# never, so this is a convergence mechanism, not a hot path: one indexed
# single-row SELECT per worker per interval.
REFRESH_INTERVAL_SECONDS = 60


async def _get_row(db: AsyncSession) -> Optional[InstanceSettings]:
    result = await db.execute(
        select(InstanceSettings).where(
            InstanceSettings.key == LICENSE_SETTING_KEY,
            InstanceSettings.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def get_stored_license(db: AsyncSession) -> dict:
    """The stored license row as a plain dict (``{}`` when nothing is stored).

    Shape: ``{"key": <jwt>, "updated_by_user_id": ..., "updated_at": <iso>}``.
    """
    row = await _get_row(db)
    if row is None:
        return {}
    return dict(row.value or {})


async def refresh_license_from_db(db: AsyncSession) -> None:
    """Mirror the stored key into ``app.ee.license`` if it differs from what we hold.

    Cheap and idempotent — when nothing changed this does not touch the decoded
    license cache, so the JWT signature is not re-verified on every tick.
    """
    stored = await get_stored_license(db)
    key = stored.get("key") or None
    if key != get_db_license_key():
        set_db_license_key(key)
        info = get_license_info()
        logger.info(
            "License key reloaded from database (source=%s, tier=%s)",
            "database" if key else "config",
            info.tier,
        )


async def save_license_key(db: AsyncSession, key: str, user_id: Optional[str]) -> LicenseInfo:
    """Validate, store, and activate a license key.

    The key is verified *before* it is written, so a typo or an already-expired
    key cannot knock a working instance down to community mode. Raises
    ``ValueError`` with a user-facing message when the key is not usable.
    """
    key = (key or "").strip()
    if not key:
        raise ValueError("License key is required.")

    info = validate_license_key(key)
    if not info.licensed:
        if info.tier == "expired":
            raise ValueError(
                "This license key has already expired. Please use a current key."
            )
        raise ValueError(
            "This license key is not valid. Check that it was copied in full."
        )

    row = await _get_row(db)
    value = {
        "key": key,
        "updated_by_user_id": user_id,
        "updated_at": datetime.utcnow().isoformat(),
    }
    if row is None:
        row = InstanceSettings(key=LICENSE_SETTING_KEY, value=value)
        db.add(row)
    else:
        row.value = value
        row.updated_at = datetime.utcnow()
        flag_modified(row, "value")
        db.add(row)
    await db.commit()

    # Activate in this worker immediately; other workers pick it up on their
    # next refresh tick.
    set_db_license_key(key)
    return get_license_info()


async def remove_license_key(db: AsyncSession, user_id: Optional[str]) -> LicenseInfo:
    """Drop the stored key, falling back to bow-config / BOW_LICENSE_KEY."""
    row = await _get_row(db)
    if row is not None:
        row.value = {
            "key": None,
            "updated_by_user_id": user_id,
            "updated_at": datetime.utcnow().isoformat(),
        }
        row.updated_at = datetime.utcnow()
        flag_modified(row, "value")
        db.add(row)
        await db.commit()

    set_db_license_key(None)
    return get_license_info()


async def load_license_at_startup() -> None:
    """Seed the mirrored key before the app serves traffic."""
    from app.dependencies import async_session_maker

    try:
        async with async_session_maker() as db:
            await refresh_license_from_db(db)
    except Exception as e:  # noqa: BLE001 — never block startup on this
        logger.warning(f"Could not load license key from database: {e}")


async def run_license_refresher(stop_event: Optional[asyncio.Event] = None) -> None:
    """Periodically re-read the stored key in this worker.

    Deliberately *not* gated on the scheduler-leader lock: that lock exists so a
    job runs once across workers, whereas every worker holds its own license
    cache and so every worker has to refresh its own.
    """
    from app.dependencies import async_session_maker

    while True:
        if stop_event is not None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=REFRESH_INTERVAL_SECONDS)
                return  # stop_event set → shutting down
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(REFRESH_INTERVAL_SECONDS)

        try:
            async with async_session_maker() as db:
                await refresh_license_from_db(db)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a bad tick must not kill the loop
            logger.warning(f"License refresh failed: {e}")
