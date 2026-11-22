"""Pick the ExternalPlatform row an outbound chat reply must go through.

An organization can hold several platforms of the same type (e.g. multiple
WhatsApp numbers). Inbound dispatch already resolves the exact instance the
message arrived on and stamps it on ``Report.external_platform_id``; replies
must leave through that same instance, or the user hears back from a
different number (or, with a bare ``scalar_one_or_none()`` lookup, the send
raises ``MultipleResultsFound`` and nothing is delivered).
"""

from typing import Optional

from sqlalchemy import select

from app.models.external_platform import ExternalPlatform


async def resolve_outbound_platform(
    db,
    organization_id: str,
    platform_type: str,
    platform_id: Optional[str] = None,
) -> Optional[ExternalPlatform]:
    """Return the platform to reply through; never raises on multiple rows.

    1. ``platform_id`` (the instance the conversation came in on), when it
       exists in this org and is of the expected type.
    2. Otherwise the oldest platform of that type — the single-instance
       behavior older reports (created before per-instance routing) rely on.
    """
    if platform_id:
        platform = (
            await db.execute(
                select(ExternalPlatform).where(
                    ExternalPlatform.id == str(platform_id),
                    ExternalPlatform.organization_id == organization_id,
                    ExternalPlatform.platform_type == platform_type,
                )
            )
        ).scalar_one_or_none()
        if platform:
            return platform

    return (
        await db.execute(
            select(ExternalPlatform)
            .where(
                ExternalPlatform.organization_id == organization_id,
                ExternalPlatform.platform_type == platform_type,
            )
            .order_by(ExternalPlatform.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
