"""Atomic persistence of per-viewer results on the supported app databases."""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import lazyload

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
    # Columns only. StepUserResult.step and .user are lazy="selectin", and the
    # chain continues through Step.query to the report graph, so a plain select
    # here sent ~22 statements and fetched the step with its stored data: ~75 ms
    # of each viewer run on a large step, for relations no caller reads.
    # populate_existing stays: the upsert bypassed the ORM, so an instance already
    # in the session must take the new values. On such a held instance it also
    # resets .step/.user to unloaded; no caller reads them after storing.
    return (await db.execute(
        select(StepUserResult).filter_by(**key)
        .options(lazyload("*"))
        .execution_options(populate_existing=True)
    )).scalar_one()


async def find_viewer_result(db, *, step_id, user_id, params_fingerprint):
    """The viewer's stored result for this step and parameter set, or None.

    Columns only. StepUserResult.step and .user are lazy="selectin" and fan out
    through Step.query to the report graph: a plain select sent ~44 statements
    (~50 ms) on every run that finds a stored row, for relations the callers
    never read (they use status, last_run_at and data).
    """
    return (await db.execute(
        select(StepUserResult).options(lazyload("*")).where(
            StepUserResult.step_id == str(step_id),
            StepUserResult.user_id == str(user_id),
            StepUserResult.params_fingerprint == params_fingerprint,
        )
    )).scalars().first()
