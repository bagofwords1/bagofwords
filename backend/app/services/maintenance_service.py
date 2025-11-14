from datetime import datetime, timedelta
from typing import Tuple
from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError
from app.dependencies import async_session_maker
from app.settings.logging_config import get_logger

logger = get_logger(__name__)

RETENTION_DAYS_DEFAULT = 14


async def purge_step_payloads_keep_latest_per_query(
    retention_days: int = RETENTION_DAYS_DEFAULT,
    null_fields: Tuple[str, ...] = ("data", "data_model", "view"),
) -> int:
    """
    Daily maintenance task:
    - For each query_id, keep only the latest-by-updated_at payload; null others.
    - If that latest is stale (created_at and updated_at both older than cutoff), purge it too.
    - Rows with NULL query_id are only purged when stale.
    - Excludes active steps ('draft', 'running').
    """
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    set_clause = ", ".join(f"{field} = NULL" for field in null_fields)
    nonnull_predicate = " OR ".join(f"s.{field} IS NOT NULL" for field in null_fields)

    sql = text(f"""
    WITH ranked AS (
      SELECT
        id,
        query_id,
        created_at,
        updated_at,
        status,
        ROW_NUMBER() OVER (
          PARTITION BY query_id
          ORDER BY updated_at DESC
        ) AS rn
      FROM steps
      WHERE status IN ('success')
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
    """)

    async with async_session_maker() as session:
        purged = 0
        try:
        result = await session.execute(sql, {"cutoff": cutoff})
        await session.commit()
        purged = result.rowcount or 0
        logger.info(
            "Purged step payloads (keep latest per query; purge stale latest)",
            extra={
                "purged": purged,
                "cutoff": cutoff.isoformat(),
                "null_fields": null_fields,
                "retention_days": retention_days,
            },
        )
        except (InterfaceError, OperationalError) as e:
            try:
                await session.rollback()
            except Exception:
                pass
            logger.warning(
                "Maintenance purge skipped due to transient DB error",
                extra={
                    "error": str(e),
                    "cutoff": cutoff.isoformat(),
                    "retention_days": retention_days,
                },
            )
        except Exception as e:
            try:
                await session.rollback()
            except Exception:
                pass
            logger.exception(
                "Maintenance purge failed unexpectedly",
                extra={
                    "error": str(e),
                    "cutoff": cutoff.isoformat(),
                    "retention_days": retention_days,
                },
            )
        return purged


