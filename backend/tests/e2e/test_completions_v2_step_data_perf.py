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
from app.services.report_service import ReportService
from app.serializers.completion_v2 import PREVIEW_ROWS
from app.ai.persisted_summary import UI_FILE_PREVIEW_CHARS
from app.schemas.report_summary_schema import ReportSummaryResponse
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


async def _seed(
    rows_per_step: int,
    *,
    tool_name: str = "create_data",
    duplicate_rows_in_tool_result: bool = False,
    legacy_summaries: bool = False,
):
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

            result_json = {"widget_data": {"rows": _make_rows(rows_per_step)}}
            if duplicate_rows_in_tool_result:
                # write_csv historically persisted the same complete dataset in
                # ToolExecution.result_json even though the created Step remains
                # the canonical, independently fetchable result. This exact
                # duplicate made a 10-completion response exceed 100 MB.
                result_json = {
                    "success": True,
                    "row_count": rows_per_step,
                    "columns": [column["field"] for column in columns],
                    "data": {
                        "rows": _make_rows(rows_per_step),
                        "columns": columns,
                        "info": {"total_rows": rows_per_step},
                    },
                    "data_model": {"type": "table", "columns": columns},
                    "view": {"type": "table"},
                }

            te = ToolExecution(
                agent_execution_id=ae.id,
                tool_name=tool_name,
                status="success",
                success=True,
                arguments_json={},
                result_json=result_json,
                created_widget_id=widget.id,
                created_step_id=step.id,
            )
            db.add(te)
            await db.flush()
            if legacy_summaries:
                # Reproduce rows written before context_summary_json existed.
                # The migration normally fills these; the defensive read path
                # must still return a bounded result if an external insert
                # leaves the summary null.
                step.context_summary_json = None
                te.context_summary_json = None

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


async def _seed_read_file(content: str, *, legacy_summary: bool = False):
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Read file org {suffix}")
        user = User(
            name="Read file user",
            email=f"read-file-{suffix}@example.com",
            hashed_password="x",
            is_active=True,
            is_verified=True,
        )
        db.add_all([org, user])
        await db.flush()
        report = Report(
            title=f"Read file report {suffix}",
            slug=f"read-file-report-{suffix}",
            status="draft",
            user_id=user.id,
            organization_id=org.id,
        )
        db.add(report)
        await db.flush()
        completion = Completion(
            prompt={"content": "read the file"},
            completion={"content": "done"},
            status="success",
            role="system",
            report_id=report.id,
            user_id=user.id,
            turn_index=0,
        )
        db.add(completion)
        await db.flush()
        execution = AgentExecution(
            completion_id=completion.id,
            organization_id=org.id,
            user_id=user.id,
            report_id=report.id,
            status="completed",
        )
        db.add(execution)
        await db.flush()
        tool = ToolExecution(
            agent_execution_id=execution.id,
            tool_name="read_file",
            status="success",
            success=True,
            arguments_json={"file_id": "logs/large.txt", "title": "Large log"},
            result_json={
                "success": True,
                "connection_id": "connection-1",
                "file_id": "logs/large.txt",
                "file_name": "large.txt",
                "path": "logs/large.txt",
                "content_type": "text",
                "text": content,
                "truncated": True,
                "session_file_id": "session-file-1",
            },
        )
        db.add(tool)
        await db.flush()
        if legacy_summary:
            tool.context_summary_json = None
        db.add(
            CompletionBlock(
                completion_id=completion.id,
                agent_execution_id=execution.id,
                source_type="tool",
                tool_execution_id=tool.id,
                block_index=0,
                title="read_file",
                status="completed",
            )
        )
        await db.commit()
        return {
            "org": org,
            "user": user,
            "report_id": str(report.id),
            "tool_id": str(tool.id),
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


@pytest.mark.e2e
def test_write_csv_duplicate_rows_are_bounded_in_report_read_payloads():
    """Timeline and summary reads must preview, not resend, tool-result rows.

    The complete rows are still available from the created Step. This guards
    both report-open endpoints because they serialize the same tool execution
    through different code paths.
    """

    async def scenario():
        small = await _seed(
            rows_per_step=10,
            tool_name="write_csv",
            duplicate_rows_in_tool_result=True,
        )
        large = await _seed(
            rows_per_step=ROWS_PER_STEP,
            tool_name="write_csv",
            duplicate_rows_in_tool_result=True,
            legacy_summaries=True,
        )

        completion_service = CompletionService()
        report_service = ReportService()

        async def payloads_for(seeded):
            async with async_session_maker() as db:
                completions = await completion_service.get_completions_v2(
                    db, seeded["report_id"], seeded["org"], seeded["user"], limit=10
                )
                summary = ReportSummaryResponse.model_validate(
                    await report_service.get_report_summary(db, seeded["report_id"])
                )

            completion_body = completions.model_dump_json()
            summary_body = summary.model_dump_json()

            completion_rows = []
            for completion in completions.completions:
                for block in completion.completion_blocks:
                    tool = block.tool_execution
                    if tool and isinstance(tool.result_json, dict):
                        completion_rows.append(
                            len((tool.result_json.get("data") or {}).get("rows") or [])
                        )

            summary_rows = [
                len((tool.result_json.get("data") or {}).get("rows") or [])
                for tool in summary.queries
                if isinstance(tool.result_json, dict)
            ]
            return len(completion_body), len(summary_body), completion_rows, summary_rows

        small_payloads = await payloads_for(small)
        large_payloads = await payloads_for(large)
        small_completion_size, small_summary_size, _, _ = small_payloads
        large_completion_size, large_summary_size, completion_rows, summary_rows = large_payloads

        print(
            "\n[write-csv-ui] completions: "
            f"small={small_completion_size / 1e3:.1f}kB "
            f"large={large_completion_size / 1e3:.1f}kB; "
            "summary: "
            f"small={small_summary_size / 1e3:.1f}kB "
            f"large={large_summary_size / 1e3:.1f}kB"
        )

        assert completion_rows and max(completion_rows) <= PREVIEW_ROWS
        assert summary_rows and max(summary_rows) <= PREVIEW_ROWS
        assert large_completion_size < max(small_completion_size * 3, 100_000)
        assert large_summary_size < max(small_summary_size * 3, 100_000)

        # Projection is a read-path optimization only: the canonical result
        # still returns every row when the visible widget asks for its Step.
        async with async_session_maker() as db:
            step = await db.get(Step, large["step_ids"][0])
            served = StepSchema.from_orm(step)
        assert len(served.data.get("rows", [])) == ROWS_PER_STEP

    _run(scenario())


@pytest.mark.e2e
def test_read_file_history_keeps_the_visible_excerpt_without_shipping_full_content():
    """The timeline retains exactly the card's visible 4k excerpt and metadata."""

    async def scenario():
        small = await _seed_read_file("s" * 1_000)
        large = await _seed_read_file("l" * 200_000, legacy_summary=True)
        service = CompletionService()

        async def payload_for(seeded):
            async with async_session_maker() as db:
                response = await service.get_completions_v2(
                    db,
                    seeded["report_id"],
                    seeded["org"],
                    seeded["user"],
                    limit=10,
                )
            tool = next(
                block.tool_execution
                for completion in response.completions
                for block in completion.completion_blocks
                if block.tool_execution is not None
            )
            return response.model_dump_json(), tool.result_json

        small_body, small_result = await payload_for(small)
        large_body, large_result = await payload_for(large)

        assert len(small_result["text"]) == 1_000
        assert len(large_result["text"]) == UI_FILE_PREVIEW_CHARS
        assert large_result["file_name"] == "large.txt"
        assert large_result["session_file_id"] == "session-file-1"
        assert len(large_body) < max(len(small_body) * 6, 20_000)

        # Projection changes only the report-list response. The audit record
        # remains complete and can still serve specialized detail workflows.
        async with async_session_maker() as db:
            stored = await db.get(ToolExecution, large["tool_id"])
            assert len(stored.result_json["text"]) == 200_000

    _run(scenario())
