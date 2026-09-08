"""Seed a load-test shaped dataset for the diagnosis explorer and time its queries.

    cd backend
    BOW_DATABASE_URL=... uv run python scripts/seed_diagnosis_load.py --org ORG_ID --runs 5000 \\
        --users USER_ID[,USER_ID...] --agents DS_ID[,DS_ID...]

Creates, directly in the database (there is no API that produces agent runs):
reports (each attached to one agent, some to two), user/system completion
pairs, agent executions with timings and errors, tool executions, usage
records (attributed to the run), feedback and judge scores — then runs the
rollup backfill over them and prints timings for the four diagnosis reads.

Every row is tagged in ``prompt_text`` with ``[load-seed]`` so a rerun can
remove the previous batch with ``--clean``.
"""
import argparse
import asyncio
import random
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TAG = "[load-seed]"
PROMPTS = [
    "Revenue by region for the current month", "Top 10 albums by revenue with a bar chart",
    "Compare Q2 vs Q3 gross margin per product line", "Which PRs were merged last week without a review?",
    "Weekly sales analysis for the marketing team", "Show customer churn by cohort for the last 12 months",
    "Active users today and the trend over the last 30 days", "Root cause the drop in production output",
    "List all issues labeled bug older than 90 days", "Average order value by channel, monthly",
    "Inventory turnover by warehouse", "Support tickets by priority this quarter",
]
TOOLS = [("describe_tables", None, 0.02), ("create_data", "execute_sql", 0.12), ("search_reports", None, 0.03),
         ("create_dashboard", None, 0.05), ("answer", None, 0.01), ("read_resources", None, 0.02)]
ERRORS = ["psycopg2.errors.UndefinedColumn: column region_name does not exist", "Query timed out after 30s",
          "division by zero", "relation weekly_sales_v2 does not exist", "permission denied for table users",
          "GitHub API rate limit exceeded (resets in 14m)"]
MODELS = [("gpt-4.1", "openai", 2.0, 8.0), ("gpt-4.1-mini", "openai", 0.4, 1.6), ("claude-haiku", "anthropic", 0.8, 4.0)]
PLATFORMS = [None] * 8 + ["slack", "teams", "email"]


async def _seed(args) -> None:
    import main  # noqa: F401
    from sqlalchemy import delete, select, text
    from app.dependencies import async_session_maker
    from app.models.agent_execution import AgentExecution
    from app.models.completion import Completion
    from app.models.completion_feedback import CompletionFeedback
    from app.models.llm_model import LLMModel
    from app.models.llm_provider import LLMProvider
    from app.models.llm_usage_record import LLMUsageRecord
    from app.models.report import Report
    from app.models.report_data_source_association import report_data_source_association as assoc
    from app.models.tool_execution import ToolExecution
    from app.services.diagnosis.rollup import backfill

    rnd = random.Random(args.seed)
    users = [u.strip() for u in args.users.split(",") if u.strip()]
    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    now = datetime.utcnow()

    async with async_session_maker() as db:
        if args.clean:
            ids = [r for r in (await db.execute(
                select(AgentExecution.id).where(AgentExecution.organization_id == args.org, AgentExecution.prompt_text.like(f"{TAG}%"))
            )).scalars().all()]
            if ids:
                await db.execute(delete(LLMUsageRecord).where(LLMUsageRecord.agent_execution_id.in_(ids)))
                await db.execute(delete(ToolExecution).where(ToolExecution.agent_execution_id.in_(ids)))
                await db.execute(delete(AgentExecution).where(AgentExecution.id.in_(ids)))
                await db.commit()
            print(f"removed {len(ids)} previously seeded runs")

        # One provider + models to hang usage records on.
        models = {}
        for model_id, provider_type, in_rate, out_rate in MODELS:
            provider = (await db.execute(select(LLMProvider).where(
                LLMProvider.organization_id == args.org, LLMProvider.provider_type == provider_type, LLMProvider.name == f"seed-{provider_type}"
            ))).scalars().first()
            if provider is None:
                provider = LLMProvider(name=f"seed-{provider_type}", provider_type=provider_type, organization_id=args.org, use_preset_credentials=False, is_enabled=False)
                db.add(provider)
                await db.flush()
            model = (await db.execute(select(LLMModel).where(LLMModel.provider_id == provider.id, LLMModel.model_id == model_id))).scalars().first()
            if model is None:
                model = LLMModel(name=model_id, model_id=model_id, provider_id=provider.id, organization_id=args.org, is_custom=True, is_enabled=False)
                db.add(model)
                await db.flush()
            models[model_id] = (model.id, provider_type, in_rate, out_rate)

        # Reports: ~1 per 4 runs; 10% draw on two agents.
        n_reports = max(1, args.runs // 4)
        reports = []
        for i in range(n_reports):
            owner = rnd.choice(users)
            r = Report(title=f"{rnd.choice(PROMPTS)[:40]} #{i}", slug=f"load-{uuid.uuid4().hex[:12]}", user_id=owner,
                       organization_id=args.org, status="published")
            db.add(r)
            await db.flush()
            ds = [rnd.choice(agents)]
            if len(agents) > 1 and rnd.random() < 0.1:
                ds.append(rnd.choice([a for a in agents if a != ds[0]]))
            for d in ds:
                await db.execute(assoc.insert().values(report_id=r.id, data_source_id=d))
            reports.append((r.id, owner, ds))
        await db.flush()

        started = time.monotonic()
        for i in range(args.runs):
            report_id, owner, _ = rnd.choice(reports)
            user_id = owner if rnd.random() < 0.8 else rnd.choice(users)
            created = now - timedelta(seconds=rnd.random() * args.days * 86400)
            is_error = rnd.random() < 0.18
            duration = rnd.lognormvariate(9.2, 0.9)  # ~10s median, long tail
            prompt = f"{TAG} {rnd.choice(PROMPTS)}"
            platform = rnd.choice(PLATFORMS)
            judged = rnd.random() < 0.7
            uc = Completion(prompt={"content": prompt}, completion={"content": ""}, role="user", message_type="user_message",
                            report_id=report_id, user_id=user_id, created_at=created, external_platform=platform,
                            response_score=rnd.choice([1, 2, 3, 4, 4, 5, 5]) if judged else 4,
                            instructions_effectiveness=rnd.choice([1, 2, 3, 4, 4, 5]) if judged else 4,
                            context_effectiveness=rnd.choice([2, 3, 4, 4, 5]) if judged else 4)
            db.add(uc)
            await db.flush()
            sc = Completion(prompt={"content": ""}, completion={"content": "done"}, role="system", parent_id=uc.id,
                            report_id=report_id, created_at=created)
            db.add(sc)
            await db.flush()
            ae = AgentExecution(completion_id=sc.id, organization_id=args.org, user_id=user_id, report_id=report_id,
                                status="error" if is_error else "success", created_at=created, started_at=created,
                                completed_at=created + timedelta(milliseconds=duration), total_duration_ms=duration,
                                first_token_ms=rnd.uniform(300, 2500), thinking_ms=rnd.uniform(500, 6000),
                                error_json={"message": rnd.choice(ERRORS)} if is_error else None,
                                bow_version="0.0.556")
            db.add(ae)
            await db.flush()

            n_tools = rnd.choice([0, 1, 2, 3, 3, 4, 5, 6])
            t0 = created
            for k in range(n_tools):
                name, action, err_rate = rnd.choice(TOOLS)
                failed = rnd.random() < (err_rate * (4 if is_error else 1))
                attempts = 2 if (failed and rnd.random() < 0.5) else 1
                for attempt in range(1, attempts + 1):
                    d = rnd.lognormvariate(7.0, 1.0)
                    db.add(ToolExecution(agent_execution_id=ae.id, tool_name=name, tool_action=action, arguments_json={},
                                         status="error" if (failed and attempt == attempts) or (failed and attempts == 2 and attempt == 1) else "success",
                                         success=not failed, started_at=t0, completed_at=t0 + timedelta(milliseconds=d),
                                         duration_ms=d, attempt_number=attempt, max_retries=2 if name == "create_data" else 0,
                                         error_message=rnd.choice(ERRORS) if failed else None, created_at=t0))
                    t0 += timedelta(milliseconds=d)

            model_id = rnd.choice(list(models))
            llm_model_id, provider_type, in_rate, out_rate = models[model_id]
            for scope in ["planner"] * rnd.choice([1, 2, 3]) + (["tool_call_judge"] if judged else []):
                pt = int(rnd.lognormvariate(8.5, 0.6))
                ct = int(rnd.lognormvariate(6.0, 0.7))
                cost = pt / 1e6 * in_rate + ct / 1e6 * out_rate
                db.add(LLMUsageRecord(scope=scope, scope_ref_id=None, organization_id=args.org, user_id=user_id, report_id=report_id,
                                      agent_execution_id=ae.id, llm_model_id=llm_model_id, model_id=model_id, provider_type=provider_type,
                                      prompt_tokens=pt, completion_tokens=ct, input_cost_usd=pt / 1e6 * in_rate,
                                      output_cost_usd=ct / 1e6 * out_rate, total_cost_usd=cost, created_at=created + timedelta(seconds=1)))
            fb = rnd.random()
            if fb < 0.08:
                db.add(CompletionFeedback(completion_id=sc.id, organization_id=args.org, user_id=user_id, direction=-1,
                                          message=rnd.choice(["wrong numbers", "missed the region filter", "too slow", None]), created_at=created))
            elif fb < 0.2:
                db.add(CompletionFeedback(completion_id=sc.id, organization_id=args.org, user_id=user_id, direction=1, created_at=created))

            if (i + 1) % 500 == 0:
                await db.commit()
                print(f"  seeded {i + 1}/{args.runs} runs ({(i + 1) / (time.monotonic() - started):.0f}/s)", flush=True)
        await db.commit()
        print(f"seeded {args.runs} runs in {time.monotonic() - started:.1f}s")

        t = time.monotonic()
        done = await backfill(db, only_missing=True, batch_size=500, organization_id=args.org)
        print(f"rolled up {done} runs in {time.monotonic() - t:.1f}s")


async def _bench(args) -> None:
    import main  # noqa: F401
    from app.dependencies import async_session_maker
    from app.services.diagnosis.service import RunQueryParams, diagnosis_service

    now = datetime.utcnow()
    queries = ["", "status:error", "tool:create_data tool.status:error", "revenue", "cost:>$0.05 duration:>10s",
               "feedback:negative", "judge.confidence:<3", "model:gpt-4.1 provider:openai", "user:nobody", "NOT status:error"]
    async with async_session_maker() as db:
        print(f"\n{'query':45} {'runs':>7} {'items+panels ms':>16} {'items only ms':>14} {'facets ms':>10}")
        for q in queries:
            rows = []
            for _ in range(3):
                p = RunQueryParams(q=q, start=now - timedelta(days=args.days), end=now + timedelta(minutes=1), limit=25)
                t = time.monotonic()
                res = await diagnosis_service.run_query(db, args.org, None, p)
                full = (time.monotonic() - t) * 1000
                p.include = {"items"}
                t = time.monotonic()
                await diagnosis_service.run_query(db, args.org, None, p)
                only = (time.monotonic() - t) * 1000
                t = time.monotonic()
                await diagnosis_service.facets(db, args.org, None, "status", p)
                fac = (time.monotonic() - t) * 1000
                rows.append((res["total"], full, only, fac))
            total, full, only, fac = rows[-1][0], min(r[1] for r in rows), min(r[2] for r in rows), min(r[3] for r in rows)
            print(f"{q or '<empty>':45} {total:7d} {full:16.0f} {only:14.0f} {fac:10.0f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--org", required=True)
    parser.add_argument("--users", default="", help="comma-separated user ids to spread runs across")
    parser.add_argument("--agents", default="", help="comma-separated data source ids to spread reports across")
    parser.add_argument("--runs", type=int, default=5000)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--clean", action="store_true", help="remove a previous seeded batch first")
    parser.add_argument("--bench-only", action="store_true", help="skip seeding, just time the queries")
    a = parser.parse_args()
    if not a.bench_only:
        if not a.users or not a.agents:
            parser.error("--users and --agents are required to seed")
        asyncio.run(_seed(a))
    asyncio.run(_bench(a))
