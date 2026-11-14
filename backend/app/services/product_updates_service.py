"""Product-updates opt-in: the instance admin's choice to receive release notes.

Self-hosted instances never send a user's email address by default. The one
exception is this explicit opt-in: the admin ticks "Send me product updates"
(on the first sign-up, on the onboarding welcome screen when they signed up
through SSO, or later in Settings → General), and only then is their email
sent to BOW, once, through the instance's telemetry channel.

The decision is instance-wide — one admin per deployment is enough to reach
it — so it lives in ``instance_settings`` (one row, key ``product_updates``).

Rules this module enforces:

* the prompt exists only while telemetry is enabled and the instance is
  self-hosted (the cloud deployment already has its users' contact details);
* nothing leaves the instance unless ``opted_in`` is true, and telemetry
  being off sends nothing at all (``Telemetry`` is a no-op then);
* opting out is sent as a flag only — never the email — so the subscription
  can be stopped on the receiving side.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.telemetry import telemetry
from app.models.instance_settings import InstanceSettings
from app.settings.config import settings

logger = logging.getLogger(__name__)

PRODUCT_UPDATES_SETTING_KEY = "product_updates"

Source = Literal["signup", "onboarding", "settings"]


def prompt_available() -> bool:
    """Whether this instance may ask for (and send) the opt-in at all.

    Reads configuration only — not ``Telemetry._enabled()``, which is also off
    under ``TESTING`` — so the UI contract is testable; the actual send still
    goes through ``Telemetry`` and is a no-op whenever telemetry is off.
    """
    try:
        cfg = settings.bow_config
        telemetry_on = bool(getattr(cfg, "telemetry", None) and cfg.telemetry.enabled)
        self_hosted = (getattr(getattr(cfg, "deployment", None), "type", "self_hosted") or "self_hosted") != "cloud"
        return telemetry_on and self_hosted
    except Exception:
        return False


async def _get_row(db: AsyncSession) -> Optional[InstanceSettings]:
    result = await db.execute(
        select(InstanceSettings).where(
            InstanceSettings.key == PRODUCT_UPDATES_SETTING_KEY,
            InstanceSettings.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def get_decision(db: AsyncSession) -> dict:
    """The stored decision, or ``{}`` when the admin has not answered yet.

    Shape: ``{"opted_in": bool, "email": str | None, "user_id": str,
    "source": "signup" | "onboarding" | "settings", "decided_at": <iso>}``.
    """
    row = await _get_row(db)
    return dict(row.value or {}) if row is not None else {}


async def record_decision(
    db: AsyncSession,
    *,
    user_id: str,
    email: Optional[str],
    opted_in: bool,
    source: Source,
) -> dict:
    """Store the admin's answer and, when it changes the subscription, send it.

    Idempotent: re-saving the same answer sends nothing new.
    """
    row = await _get_row(db)
    previous = dict(row.value or {}) if row is not None else {}

    value = {
        "opted_in": bool(opted_in),
        # Kept locally only while subscribed, so Settings can show which
        # address receives the updates.
        "email": (email or None) if opted_in else None,
        "user_id": str(user_id),
        "source": source,
        "decided_at": datetime.utcnow().isoformat(),
    }
    if row is None:
        db.add(InstanceSettings(key=PRODUCT_UPDATES_SETTING_KEY, value=value))
    else:
        row.value = value
        row.updated_at = datetime.utcnow()
        flag_modified(row, "value")
        db.add(row)
    await db.commit()

    # Send only when the subscription actually changes: subscribing (or a new
    # address), or leaving an existing subscription. Declining when nothing
    # was ever subscribed sends nothing at all.
    was_subscribed = bool(previous.get("opted_in"))
    changed = (
        (value["opted_in"] and (not was_subscribed or previous.get("email") != value["email"]))
        or (not value["opted_in"] and was_subscribed)
    )
    if changed and prompt_available():
        await _send(value)
    return value


async def _send(value: dict) -> None:
    """Report the subscription change. ``Telemetry`` drops it when disabled."""
    user_id = value["user_id"]
    try:
        if value["opted_in"] and value.get("email"):
            await telemetry.identify(
                user_id, {"email": value["email"], "product_updates": True}
            )
            await telemetry.capture(
                "product_updates_subscribed", {"source": value["source"]}, user_id=user_id
            )
        elif not value["opted_in"]:
            await telemetry.identify(user_id, {"product_updates": False})
            await telemetry.capture(
                "product_updates_unsubscribed", {"source": value["source"]}, user_id=user_id
            )
    except Exception:
        logger.exception("product_updates: failed to report subscription change")


async def record_signup_choice(db: AsyncSession, user, opted_in: Optional[bool]) -> None:
    """Apply the sign-up checkbox, but only for the instance's first user.

    The sign-up page shows the checkbox only while the instance is unclaimed;
    the server re-checks here (exactly one user now exists) so a crafted
    request from a later, invited user cannot set the instance's answer.
    """
    if opted_in is None or not prompt_available():
        return
    from app.models.user import User

    user_count = await db.scalar(select(func.count()).select_from(User))
    if user_count != 1:
        return
    if await get_decision(db):
        return
    await record_decision(
        db,
        user_id=str(user.id),
        email=user.email,
        opted_in=bool(opted_in),
        source="signup",
    )
