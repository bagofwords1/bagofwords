"""Seed a published dashboard's copies (phase 2, part B).

A release copy is a query, its step and its visualization that belong to a
published dashboard (`artifact_release_id`) instead of a conversation: no
report and no widget. Nothing creates them yet (Publish is phase 5); the
tests seed them directly to prove that no conversation path reaches them.
"""
import uuid

from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.artifact_release import ArtifactRelease
from app.models.query import Query
from app.models.step import Step
from app.models.visualization import Visualization

# Every copy carries this value, so a response that leaked it is easy to spot.
MARKER = "RELEASE-COPY-ROW"
COPY_CODE = (
    "def generate_df(ds_clients, excel_files):\n"
    "    import pandas as pd\n"
    f"    return pd.DataFrame({{'secret': ['{MARKER}']}})\n"
)


async def add_release_copy(*, artifact_id: str, organization_id: str, user_id: str,
                           code: str = COPY_CODE, with_default_step: bool = True) -> dict:
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        release = ArtifactRelease(artifact_id=artifact_id, organization_id=organization_id,
                                  content={"code": "function App() {}", "visualization_ids": []},
                                  status="ready", published_by=user_id)
        db.add(release)
        await db.flush()
        query = Query(title="copy", report_id=None, widget_id=None, artifact_release_id=release.id,
                      organization_id=organization_id, user_id=user_id)
        db.add(query)
        await db.flush()
        step = Step(title="copy", slug=f"copy-{suffix}", status="success", widget_id=None,
                    query_id=query.id, code=code,
                    data={"rows": [{"secret": MARKER}], "columns": [{"field": "secret"}]})
        db.add(step)
        await db.flush()
        if with_default_step:
            query.default_step_id = step.id
        viz = Visualization(title="copy", status="success", report_id=None,
                            artifact_release_id=release.id, query_id=query.id, view={"type": "table"})
        db.add(viz)
        await db.commit()
        return {"release": str(release.id), "query": str(query.id), "step": str(step.id),
                "viz": str(viz.id)}


async def copy_rows(ids: dict) -> dict:
    """Every column of the copy rows, to prove a request changed nothing."""
    async with async_session_maker() as db:
        out = {}
        for model, key in ((Query, "query"), (Step, "step"), (Visualization, "viz")):
            row = (await db.execute(select(model.__table__).where(model.__table__.c.id == ids[key]))).mappings().one()
            out[key] = {k: (v if not hasattr(v, "isoformat") else v.isoformat()) for k, v in row.items()}
        return out
