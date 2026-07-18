"""
Perf reproduction + regression guard: opening an authenticated report
(`/reports/{id}`) was slow because GET /api/reports/{id}/completions embedded
the FULL result set (steps.data.rows) of every widget/step created in the last
N completions. That list ships on every open, on the 15s scheduled poll, and
after every stream — so a report whose recent turns produced large datasets
re-serialised and re-shipped megabytes each time.

The fix (serializers/completion_v2.py: serialize_block_v2_sync) embeds only a
small PREVIEW of each step's rows (PREVIEW_ROWS) plus a ``truncated`` marker, so
the card paints instantly; the client lazy-fetches the complete set per visible
widget via GET /api/steps/{id} only when a result is marked truncated.

This test seeds the block graph directly (no LLM) and asserts the FIXED
behavior: the completions payload stays tiny regardless of how much data the
steps hold (only a bounded preview per step), while the row data is still
intact in the DB and served by the single-step endpoint.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      .venv/bin/python -m pytest tests/e2e/test_completions_v2_step_data_perf.py -v -s

Tune dataset size with BOW_REPRO_ROWS (rows per step, default 15000).
"""
import asyncio
import json
import os
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.report import Report
from app.models.widget import Widget
from app.models.query import Query
from app.models.step import Step
from app.models.completion import Completion
from app.models.agent_execution import AgentExecution
from app.models.tool_execution import ToolExecution
from app.models.completion_block import CompletionBlock
from app.services.completion_service import CompletionService
from app.serializers.completion_v2 import PREVIEW_ROWS
from app.schemas.step_schema import StepSchema


ROWS_PER_STEP = int(os.environ.get("BOW_REPRO_ROWS", "15000"))
N_COMPLETIONS = 6  # a handful of recent turns, each having created a dataset


def _run(coro):
    return asyncio.run(coro)


def _make_rows(n):
    return [
        {
            "order_id": i,
            "customer_name": f"customer_{i % 997}",
            "region": ("north", "south", "east", "west")[i % 4],
            "revenue": round(((i * 7) % 40 + 1) * (3.5 + (i % 900) * 0.13), 2),
            "order_date": f"2025-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}",
        }
        for i in range(n)
    ]


async def _seed(rows_per_step: int):
    suffix = uuid.uuid4().hex[:8]
    columns = [{"field": f} for f in _make_rows(1)[0].keys()]

    async with async_session_maker() as db:
        org = Organization(name=f"CV2 Org {suffix}")
        db.add(org)
        await db.flush()

        user = User(
            name="CV2 User",
            email=f"cv2-{suffix}@example.com",
            hashed_password="x",
            is_active=True,
            is_superuser=False,
            is_verified=True,
        )
        db.add(user)
        await db.flush()

        report = Report(
            title=f"CV2 Report {suffix}",
            slug=f"cv2-report-{suffix}",
            status="draft",
            user_id=user.id,
            organization_id=org.id,
        )
        db.add(report)
        await db.flush()

        step_ids = []
        for ci in range(N_COMPLETIONS):
            widget = Widget(title=f"W{ci}", slug=f"w{ci}-{suffix}", report_id=report.id)
            db.add(widget)
            await db.flush()

            query = Query(
                title=f"Q{ci}", report_id=report.id, widget_id=widget.id,
                organization_id=org.id, user_id=user.id,
            )
            db.add(query)
            await db.flush()

            step = Step(
                title=f"Step {ci}",
                slug=f"s{ci}-{suffix}",
                status="success",
                widget_id=widget.id,
                query_id=query.id,
                data={"rows": _make_rows(rows_per_step), "columns": columns},
                data_model={"type": "bar_chart", "columns": columns},
                view={"type": "bar_chart"},
            )
            db.add(step)
            await db.flush()
            query.default_step_id = step.id
            step_ids.append(str(step.id))

            completion = Completion(
                prompt={"content": f"make chart {ci}"},
                completion={"content": f"done {ci}"},
                status="success",
                role="system",
                report_id=report.id,
                user_id=user.id,
                turn_index=ci,
            )
            db.add(completion)
            await db.flush()

            ae = AgentExecution(
                completion_id=completion.id,
                organization_id=org.id,
                user_id=user.id,
                report_id=report.id,
                status="completed",
            )
            db.add(ae)
            await db.flush()

            te = ToolExecution(
                agent_execution_id=ae.id,
                tool_name="create_data",
                status="success",
                success=True,
                arguments_json={},
                result_json={"widget_data": {"rows": _make_rows(rows_per_step)}},
                created_widget_id=widget.id,
                created_step_id=step.id,
            )
            db.add(te)
            await db.flush()

            db.add(CompletionBlock(
                completion_id=completion.id,
                agent_execution_id=ae.id,
                source_type="tool",
                tool_execution_id=te.id,
                block_index=0,
                title="create_data",
                status="completed",
            ))

        await db.commit()

        step_bytes = len(json.dumps({"rows": _make_rows(rows_per_step), "columns": columns}))
        return {
            "org": org,
            "user": user,
            "report_id": str(report.id),
            "step_ids": step_ids,
            "step_bytes": step_bytes,
            "embedded_rows_bytes": step_bytes * N_COMPLETIONS,
        }


@pytest.mark.e2e
def test_completions_v2_embeds_only_a_bounded_preview():
    async def scenario():
        small = await _seed(rows_per_step=10)  # <= PREVIEW_ROWS -> ships in full
        large = await _seed(rows_per_step=ROWS_PER_STEP)

        svc = CompletionService()

        async def payload_for(seeded):
            async with async_session_maker() as db:
                resp = await svc.get_completions_v2(
                    db, seeded["report_id"], seeded["org"], seeded["user"], limit=10
                )
            body = resp.model_dump_json()
            max_rows = 0          # most rows embedded for any single step
            truncated_flags = []  # (truncated_bool, total_rows) per created_step
            for c in resp.completions:
                for b in c.completion_blocks:
                    te = b.tool_execution
                    if not te:
                        continue
                    cs = te.created_step
                    if cs is not None:
                        d = cs.data or {}
                        max_rows = max(max_rows, len(d.get("rows", [])))
                        truncated_flags.append((bool(d.get("truncated")), d.get("total_rows")))
                        # small chart config must survive so the card can lay out
                        assert cs.data_model, "data_model (chart config) was dropped"
                        assert (te.created_step_id or cs.id), "created_step_id missing — client can't lazy-fetch"
            return len(body), max_rows, truncated_flags

        small_size, small_max, small_flags = await payload_for(small)
        large_size, large_max, large_flags = await payload_for(large)

        print(f"\n[completions-v2] per-step dataset: small={small['step_bytes']/1e3:.0f}kB "
              f"large={large['step_bytes']/1e6:.1f}MB  ({N_COMPLETIONS} recent turns)")
        print(f"[completions-v2] if full rows were embedded, payload would be "
              f"~{large['embedded_rows_bytes']/1e6:.1f}MB")
        print(f"[completions-v2] actual payload: small={small_size/1e3:.1f}kB "
              f"large={large_size/1e3:.1f}kB  (max rows/step: small={small_max} large={large_max})")

        # --- REGRESSION GUARDS (post-fix behavior) ---------------------------
        # Large steps embed at most a bounded preview, flagged truncated with the
        # true total so the client knows to fetch the rest.
        assert large_max <= PREVIEW_ROWS, (
            f"large step embedded {large_max} rows inline (> preview cap {PREVIEW_ROWS})")
        assert large_flags and all(t and total == ROWS_PER_STEP for t, total in large_flags), (
            f"large steps must be marked truncated with total_rows={ROWS_PER_STEP}; got {large_flags}")
        # Small steps (<= preview cap) ship whole, no truncation, no follow-up fetch.
        assert small_max == 10 and all(not t for t, _ in small_flags), (
            f"small steps should ship in full untruncated; got {small_flags}")
        # Payload size must NOT scale with stored step data.
        assert large_size < max(small_size * 3, 80_000), (
            f"completions payload scales with stored data (small={small_size}B "
            f"large={large_size}B) — the full-embed regression is back")
        # The full payload stays a tiny fraction of the full dataset.
        assert large_size < large["embedded_rows_bytes"] * 0.1

        # --- The rows are still intact and served by the single-step endpoint.
        async with async_session_maker() as db:
            step = await db.get(Step, large["step_ids"][0])
            served = StepSchema.from_orm(step)
        assert len(served.data.get("rows", [])) == ROWS_PER_STEP, (
            "GET /api/steps/{id} must still serve the full result set")
        print(f"[completions-v2] /api/steps/{{id}} still serves "
              f"{len(served.data['rows'])} rows on demand — lazy hydration path intact")

    _run(scenario())
