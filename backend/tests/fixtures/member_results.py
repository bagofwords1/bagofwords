"""A member's stored viewer results, for tests that run the real purge after a change to their groups.

The writers of group memberships (SCIM, LDAP and OIDC sync, an invite) purge
the stored results a change to someone's groups outdates
(app/services/viewer_result_freshness.purge_members_results). These helpers
seed such results and read back what the purge left. The results belong to
an identity-scoped step (refs "i") and carry a stamp of what their runs read
then, which no stamp taken now equals: once the member's purge runs, their
latest result stays cleared (its values kept, no rows) and the older ones
are gone. Seeded directly: a viewer's run needs a reachable data source.
"""
import asyncio
import uuid
from datetime import datetime, timedelta

from app.dependencies import async_session_maker

STEP_CODE = """
def generate_df(ds_clients, excel_files, params=None):
    import pandas as pd
    return pd.DataFrame({"month": ["2024-01"]})
"""


async def seed_member_results(organization_id: str, user_id: str, n: int = 3) -> str:
    """A conversation of `user_id`'s in `organization_id` with one step, and
    `n` stored results of theirs for it (fingerprints p0..p{n-1}, p{n-1} the
    latest). Returns the step id."""
    from sqlalchemy import insert

    from app.models.query import Query
    from app.models.report import Report
    from app.models.step import Step
    from app.models.step_user_result import StepUserResult
    from app.models.widget import Widget

    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = Report(title=f"Purged {suffix}", slug=f"purged-{suffix}", user_id=str(user_id),
                        organization_id=str(organization_id))
        db.add(report)
        await db.flush()
        widget = Widget(title=f"W {suffix}", slug=f"w-{suffix}", report_id=report.id)
        db.add(widget)
        await db.flush()
        query = Query(title="Q", report_id=report.id, widget_id=widget.id,
                      organization_id=str(organization_id), user_id=str(user_id))
        db.add(query)
        await db.flush()
        step = Step(title="S", slug=f"s-{suffix}", status="success", widget_id=widget.id,
                    query_id=query.id, code=STEP_CODE)
        db.add(step)
        await db.flush()
        start = datetime.utcnow() - timedelta(days=1)
        await db.execute(insert(StepUserResult), [
            {"step_id": str(step.id), "user_id": str(user_id), "organization_id": str(organization_id),
             "report_id": str(report.id), "status": "success", "executed_as": "viewer",
             "params_fingerprint": f"p{i}", "data": {"rows": [{"month": "2024-01"}], "columns": [{"field": "month"}]},
             "last_run_at": start + timedelta(seconds=i), "source_stamp": "read-then", "source_refs": "i"}
            for i in range(n)])
        await db.commit()
        return str(step.id)


async def member_results(step_id: str) -> list:
    """(fingerprint, stamp, rows cleared) per stored result of the step."""
    from sqlalchemy import text

    async with async_session_maker() as db:
        return sorted((fp, stamp, bool(cleared)) for fp, stamp, cleared in (await db.execute(text(
            "SELECT params_fingerprint, source_stamp, data IS NULL FROM step_user_results WHERE step_id = :s"),
            {"s": step_id})).all())


def seed_member_results_now(organization_id: str, user_id: str, n: int = 3) -> str:
    """seed_member_results, from synchronous test code."""
    return asyncio.run(seed_member_results(organization_id, user_id, n))


def member_results_now(step_id: str) -> list:
    """member_results, from synchronous test code."""
    return asyncio.run(member_results(step_id))


def purged(n: int = 3) -> list:
    """What a member's purge leaves of n seeded results: the latest, cleared."""
    return [(f"p{n - 1}", "cleared", True)]


def untouched(n: int = 3) -> list:
    """n seeded results no purge reached."""
    return [(f"p{i}", "read-then", False) for i in range(n)]
