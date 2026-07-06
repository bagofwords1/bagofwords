from datetime import datetime, timedelta
from typing import Tuple, Dict
from sqlalchemy import text, select
from sqlalchemy.exc import InterfaceError, OperationalError
from app.dependencies import async_session_maker
from app.settings.logging_config import get_logger
from app.models.organization import Organization
from app.models.organization_settings import OrganizationSettings
from app.ee.license import has_feature

logger = get_logger(__name__)

RETENTION_DAYS_DEFAULT = 14


async def get_all_organization_retention_settings() -> Dict[str, int]:
    """
    Get retention days for all organizations.
    Returns dict of {org_id: retention_days}.
    Non-enterprise orgs always get the default.
    """
    async with async_session_maker() as session:
        result = await session.execute(
            select(Organization.id, OrganizationSettings.config)
            .outerjoin(OrganizationSettings,
                       Organization.id == OrganizationSettings.organization_id)
            .where(Organization.deleted_at.is_(None))
        )

        retention_map = {}
        is_enterprise = has_feature("step_retention_config")

        for org_id, config in result.all():
            if is_enterprise and config:
                step_retention = config.get('step_retention_days', {})
                if isinstance(step_retention, dict):
                    retention_map[org_id] = step_retention.get('value', RETENTION_DAYS_DEFAULT)
                else:
                    retention_map[org_id] = RETENTION_DAYS_DEFAULT
            else:
                retention_map[org_id] = RETENTION_DAYS_DEFAULT

        return retention_map


async def purge_step_payloads_for_organization(
    organization_id: str,
    retention_days: int = RETENTION_DAYS_DEFAULT,
    null_fields: Tuple[str, ...] = ("data", "data_model", "view"),
) -> int:
    """
    Purge step payloads for a specific organization.
    - For each query_id, keep only the latest-by-updated_at payload; null others.
    - If that latest is stale (created_at and updated_at both older than cutoff), purge it too.
    - Rows with NULL query_id are only purged when stale.
    - Excludes active steps ('draft', 'running').
    - Excludes steps from reports shared in ANY mode: the visibility source of
      truth (artifact_visibility / conversation_visibility != 'none') as well
      as the legacy sync fields (conversation_share_enabled = True,
      status = 'published') — a shared dashboard must never lose its data.
    """
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    set_clause = ", ".join(f"{field} = NULL" for field in null_fields)
    nonnull_predicate = " OR ".join(f"s.{field} IS NOT NULL" for field in null_fields)

    sql = text(f"""
    WITH ranked AS (
      SELECT
        s.id,
        s.query_id,
        s.created_at,
        s.updated_at,
        s.status,
        rep.conversation_share_enabled,
        rep.status AS rep_status,
        rep.artifact_visibility AS rep_artifact_visibility,
        rep.conversation_visibility AS rep_conversation_visibility,
        ROW_NUMBER() OVER (
          PARTITION BY s.query_id
          ORDER BY s.updated_at DESC
        ) AS rn
      FROM steps s
      JOIN widgets w ON s.widget_id = w.id
      JOIN reports rep ON w.report_id = rep.id
      WHERE rep.organization_id = :org_id
        AND s.status IN ('success')
    )
    UPDATE steps AS s
    SET {set_clause}
    FROM ranked r
    WHERE r.id = s.id
      AND (
            (r.query_id IS NOT NULL AND r.rn > 1)
         OR (s.created_at < :cutoff AND s.updated_at < :cutoff)
      )
      AND ({nonnull_predicate})
      AND (r.conversation_share_enabled IS NOT TRUE)
      AND (r.rep_status IS DISTINCT FROM 'published')
      AND (r.rep_artifact_visibility IS NULL OR r.rep_artifact_visibility = 'none')
      AND (r.rep_conversation_visibility IS NULL OR r.rep_conversation_visibility = 'none')
    """)

    async with async_session_maker() as session:
        purged = 0
        try:
            result = await session.execute(sql, {
                "cutoff": cutoff,
                "org_id": organization_id
            })
            await session.commit()
            purged = result.rowcount or 0
            if purged > 0:
                logger.debug(
                    "Purged step payloads for organization",
                    extra={
                        "organization_id": organization_id,
                        "purged": purged,
                        "cutoff": cutoff.isoformat(),
                        "retention_days": retention_days,
                    },
                )
        except (InterfaceError, OperationalError) as e:
            try:
                await session.rollback()
            except Exception:
                pass
            logger.warning(
                "Maintenance purge skipped for org due to transient DB error",
                extra={
                    "organization_id": organization_id,
                    "error": str(e),
                },
            )
        except Exception as e:
            try:
                await session.rollback()
            except Exception:
                pass
            logger.exception(
                "Maintenance purge failed for org",
                extra={
                    "organization_id": organization_id,
                    "error": str(e),
                },
            )
        return purged


async def purge_step_payloads_per_organization(
    null_fields: Tuple[str, ...] = ("data", "data_model", "view"),
) -> int:
    """
    Daily maintenance task that respects per-org retention settings.
    Iterates through all organizations and applies their configured retention.
    """
    retention_settings = await get_all_organization_retention_settings()
    total_purged = 0

    for org_id, retention_days in retention_settings.items():
        purged = await purge_step_payloads_for_organization(
            organization_id=org_id,
            retention_days=retention_days,
            null_fields=null_fields
        )
        total_purged += purged

    logger.info(
        "Completed per-organization step payload purge",
        extra={
            "total_purged": total_purged,
            "organizations_processed": len(retention_settings),
        }
    )
    return total_purged


async def purge_step_payloads_keep_latest_per_query(
    retention_days: int = RETENTION_DAYS_DEFAULT,
    null_fields: Tuple[str, ...] = ("data", "data_model", "view"),
) -> int:
    """
    Daily maintenance task - now delegates to per-organization purge.
    The retention_days parameter is ignored in favor of per-org settings.
    """
    import asyncio
    from app.core.scheduler import claim_scheduled_run
    # Fires in every worker (shared job store); claim so only one purges.
    if not await asyncio.to_thread(claim_scheduled_run, "purge_step_payloads_daily"):
        return 0
    return await purge_step_payloads_per_organization(null_fields=null_fields)
