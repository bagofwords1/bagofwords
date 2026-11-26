"""Atomic persistence of per-viewer results on the supported app databases."""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import lazyload

from app.models.step_user_result import StepUserResult


async def upsert_viewer_row(db, model, key: dict, values: dict, **insert_only) -> None:
    """Write one viewer's row of `model` at its unique `key`, not committed.

    A select-then-insert races when two first runs finish together; this is
    one INSERT ... ON CONFLICT (key) DO UPDATE, so both succeed and the later
    one's `values` are kept. The unique slot remains viewer + subject + params;
    a conflict must never broaden it. `insert_only` columns are written by
    the insert alone."""
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise ValueError("Unsupported application database for viewer results")

    stmt = insert(model).values(**key, **insert_only, **values)
    stmt = stmt.on_conflict_do_update(
        index_elements=list(key),
        set_={name: getattr(stmt.excluded, name) for name in values},
    )
    await db.execute(stmt)


def viewer_row(model, **key):
    """The select of a viewer's row at `key`, columns only, refreshing an
    instance the session already holds.

    Columns only: the per-viewer models' relations are lazy="selectin" and fan
    out to the report graph, for relations no caller reads. populate_existing:
    the upsert bypasses the ORM (and may run on another session), so an
    instance already in the session must take the stored values."""
    return (select(model).filter_by(**key)
            .options(lazyload("*"))
            .execution_options(populate_existing=True))


async def store_viewer_result(db, *, step_id, user_id, params_fingerprint, **values):
    key = dict(step_id=str(step_id), user_id=str(user_id),
               params_fingerprint=params_fingerprint)
    await upsert_viewer_row(db, StepUserResult, key, {**values, "updated_at": datetime.utcnow()})
    await db.commit()
    # A plain select here sent ~22 statements and fetched the step with its
    # stored data: ~75 ms of each viewer run on a large step. On an instance the
    # session holds, populate_existing also resets .step/.user to unloaded; no
    # caller reads them after storing.
    return (await db.execute(viewer_row(StepUserResult, **key))).scalar_one()


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
