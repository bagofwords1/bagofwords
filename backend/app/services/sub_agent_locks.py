"""Cross-database report serialization without changing activity timestamps."""
from sqlalchemy import update

from app.models.report import Report
from app.models.sub_agent import SubAgentWave


async def lock_report(db, report_id):
    # SQLite has no SELECT FOR UPDATE. An explicit unchanged updated_at also
    # prevents SQLAlchemy's onupdate default from turning a lock into activity.
    await db.execute(update(Report).where(Report.id == report_id).values(
        last_activity_at=Report.last_activity_at, updated_at=Report.updated_at,
    ))


async def lock_wave(db, wave_id):
    """Acquire after lock_report; preserve timestamps on both database engines."""
    await db.execute(update(SubAgentWave).where(SubAgentWave.id == wave_id).values(
        total=SubAgentWave.total, updated_at=SubAgentWave.updated_at,
    ))
