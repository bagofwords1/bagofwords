"""Atomic persistence of per-viewer results on the supported app databases."""
from datetime import datetime

from sqlalchemy import select

from app.models.step_user_result import StepUserResult


async def store_viewer_result(db, *, step_id, user_id, params_fingerprint, **values):
    # A select-then-insert races when two first runs finish together. The unique
    # slot remains viewer + step + params; a conflict must never broaden it.
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise ValueError("Unsupported application database for viewer results")

    key = dict(step_id=str(step_id), user_id=str(user_id),
               params_fingerprint=params_fingerprint)
    values = {**values, "updated_at": datetime.utcnow()}
    stmt = insert(StepUserResult).values(**key, **values)
    stmt = stmt.on_conflict_do_update(
        index_elements=list(key),
        set_={name: getattr(stmt.excluded, name) for name in values},
    )
    await db.execute(stmt)
    await db.commit()
    return (await db.execute(
        select(StepUserResult).filter_by(**key).execution_options(populate_existing=True)
    )).scalar_one()
