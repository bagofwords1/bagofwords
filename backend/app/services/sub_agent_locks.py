"""Cross-database report serialization without changing activity timestamps."""
from sqlalchemy import update

from app.models.report import Report


async def lock_report(db, report_id):
    # SQLite has no SELECT FOR UPDATE. An explicit unchanged updated_at also
    # prevents SQLAlchemy's onupdate default from turning a lock into activity.
    await db.execute(update(Report).where(Report.id == report_id).values(
        last_activity_at=Report.last_activity_at, updated_at=Report.updated_at,
    ))
