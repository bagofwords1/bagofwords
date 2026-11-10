"""The stamp a viewer's run would store, for tests that seed that run directly.

A viewer's stored result is served only while its stamp matches the current
state of what its run read (app/services/viewer_result_freshness.py). Tests
that write a viewer's result directly -- because the run itself needs a
provider the suite cannot reach -- give it the stamp the run would have
stored now, computed by the same function the run calls.
"""
import asyncio

from app.dependencies import async_session_maker


async def stamp_for(step_id: str, user_id: str, executed_as: str = "viewer") -> dict:
    """{"source_stamp", "source_refs"} of a run of `step_id` for `user_id`,
    as a conversation's or a published copy's run would store them now."""
    from app.models.artifact_release import ArtifactRelease
    from app.models.query import Query
    from app.models.step import Step
    from app.services.row_owner import step_report_id
    from app.services.viewer_result_freshness import stamp_columns

    async with async_session_maker() as db:
        step = await db.get(Step, str(step_id))
        query = await db.get(Query, str(step.query_id))
        if query.artifact_release_id:
            release = await db.get(ArtifactRelease, str(query.artifact_release_id))
            return await stamp_columns(db, step, user_id=str(user_id), organization_id=str(release.organization_id),
                                       executed_as=executed_as, release=release)
        return await stamp_columns(db, step, user_id=str(user_id), organization_id=str(query.organization_id),
                                   executed_as=executed_as, report_id=await step_report_id(db, step))


def stamp_now(step_id: str, user_id: str, executed_as: str = "viewer") -> dict:
    """stamp_for, from synchronous test code."""
    return asyncio.run(stamp_for(step_id, user_id, executed_as))
