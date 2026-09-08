import os
import pytest
from datetime import datetime, timedelta

@pytest.fixture
def get_console_metrics(test_client):
    def _get_console_metrics(user_token=None, org_id=None, start_date=None, end_date=None):
        headers = {}
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"
        if org_id:
            headers["X-Organization-Id"] = str(org_id)
        
        params = {}
        if start_date:
            params["start_date"] = start_date.isoformat()
        if end_date:
            params["end_date"] = end_date.isoformat()
        
        response = test_client.get(
            "/api/console/metrics",
            headers=headers,
            params=params
        )
        return response
    
    return _get_console_metrics

@pytest.fixture
def get_console_metrics_comparison(test_client):
    def _get_console_metrics_comparison(user_token=None, org_id=None, start_date=None, end_date=None):
        headers = {}
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"
        if org_id:
            headers["X-Organization-Id"] = str(org_id)
        
        params = {}
        if start_date:
            params["start_date"] = start_date.isoformat()
        if end_date:
            params["end_date"] = end_date.isoformat()
        
        response = test_client.get(
            "/api/console/metrics/comparison",
            headers=headers,
            params=params
        )
        return response
    
    return _get_console_metrics_comparison

@pytest.fixture
def get_timeseries_metrics(test_client):
    def _get_timeseries_metrics(user_token=None, org_id=None, start_date=None, end_date=None):
        headers = {}
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"
        if org_id:
            headers["X-Organization-Id"] = str(org_id)
        
        params = {}
        if start_date:
            params["start_date"] = start_date.isoformat()
        if end_date:
            params["end_date"] = end_date.isoformat()
        
        response = test_client.get(
            "/api/console/metrics/timeseries",
            headers=headers,
            params=params
        )
        return response
    
    return _get_timeseries_metrics

@pytest.fixture
def get_table_usage_metrics(test_client):
    def _get_table_usage_metrics(user_token=None, org_id=None, start_date=None, end_date=None):
        headers = {}
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"
        if org_id:
            headers["X-Organization-Id"] = str(org_id)
        
        params = {}
        if start_date:
            params["start_date"] = start_date.isoformat()
        if end_date:
            params["end_date"] = end_date.isoformat()
        
        response = test_client.get(
            "/api/console/metrics/table-usage",
            headers=headers,
            params=params
        )
        return response
    
    return _get_table_usage_metrics

@pytest.fixture
def get_top_users_metrics(test_client):
    def _get_top_users_metrics(user_token=None, org_id=None, start_date=None, end_date=None):
        headers = {}
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"
        if org_id:
            headers["X-Organization-Id"] = str(org_id)
        
        params = {}
        if start_date:
            params["start_date"] = start_date.isoformat()
        if end_date:
            params["end_date"] = end_date.isoformat()
        
        response = test_client.get(
            "/api/console/metrics/top-users",
            headers=headers,
            params=params
        )
        return response
    
    return _get_top_users_metrics

@pytest.fixture
def get_tool_usage_metrics(test_client):
    def _get_tool_usage_metrics(user_token=None, org_id=None, start_date=None, end_date=None):
        headers = {}
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"
        if org_id:
            headers["X-Organization-Id"] = str(org_id)
        params = {}
        if start_date:
            params["start_date"] = start_date.isoformat()
        if end_date:
            params["end_date"] = end_date.isoformat()
        response = test_client.get(
            "/api/console/metrics/tool-usage",
            headers=headers,
            params=params
        )
        return response
    
    return _get_tool_usage_metrics

@pytest.fixture
def get_llm_usage_metrics(test_client):
    def _get_llm_usage_metrics(user_token=None, org_id=None, start_date=None, end_date=None):
        headers = {}
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"
        if org_id:
            headers["X-Organization-Id"] = str(org_id)
        params = {}
        if start_date:
            params["start_date"] = start_date.isoformat()
        if end_date:
            params["end_date"] = end_date.isoformat()
        response = test_client.get(
            "/api/console/metrics/llm-usage",
            headers=headers,
            params=params
        )
        return response

    return _get_llm_usage_metrics

@pytest.fixture
def get_recent_negative_feedback(test_client):
    def _get_recent_negative_feedback(user_token=None, org_id=None, start_date=None, end_date=None):
        headers = {}
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"
        if org_id:
            headers["X-Organization-Id"] = str(org_id)
        
        params = {}
        if start_date:
            params["start_date"] = start_date.isoformat()
        if end_date:
            params["end_date"] = end_date.isoformat()
        
        response = test_client.get(
            "/api/console/metrics/recent-negative-feedback",
            headers=headers,
            params=params
        )
        return response
    
    return _get_recent_negative_feedback



@pytest.fixture
def get_diagnosis_dashboard_metrics(test_client):
    def _get_diagnosis_dashboard_metrics(user_token=None, org_id=None, start_date=None, end_date=None, user_ids=None):
        headers = {}
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"
        if org_id:
            headers["X-Organization-Id"] = str(org_id)

        params = {}
        if start_date:
            params["start_date"] = start_date.isoformat()
        if end_date:
            params["end_date"] = end_date.isoformat()
        if user_ids:
            params["user_ids"] = user_ids
        
        response = test_client.get(
            "/api/console/diagnosis/metrics",
            headers=headers,
            params=params
        )
        return response
    
    return _get_diagnosis_dashboard_metrics

@pytest.fixture
def get_agent_execution_summaries(test_client):
    def _get_agent_execution_summaries(user_token=None, org_id=None, start_date=None, end_date=None,
                                      page=1, page_size=20, filter=None, user_ids=None, prompt_search=None,
                                      data_source_ids=None):
        headers = {}
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"
        if org_id:
            headers["X-Organization-Id"] = str(org_id)

        params = {"page": page, "page_size": page_size}
        if start_date:
            params["start_date"] = start_date.isoformat()
        if end_date:
            params["end_date"] = end_date.isoformat()
        if filter:
            params["filter"] = filter
        if user_ids:
            params["user_ids"] = user_ids
        if prompt_search:
            params["prompt_search"] = prompt_search
        if data_source_ids:
            params["data_source_ids"] = data_source_ids

        response = test_client.get(
            "/api/console/agent_executions/summaries",
            headers=headers,
            params=params
        )
        return response

    return _get_agent_execution_summaries

@pytest.fixture
def seed_agent_executions():
    """Insert agent executions (each with its user→system completion pair)
    directly into the test database, optionally with the rows the diagnosis
    explorer rolls up: tool executions, feedback, judge scores, usage records.

    Direct DB writes are a last resort per tests/AGENTS.md — agent executions
    are only ever produced by a live agent run (an LLM boundary that e2e tests
    must not cross), so there is no API surface that can create them.

    Each run dict accepts: user_id, prompt, created_at, status, duration_ms,
    first_token_ms, thinking_ms, error, platform, is_eval_run,
    judge={"response", "instructions", "context"},
    feedback={"direction", "message"},
    tools=[{"name", "action", "status", "duration_ms", "attempt", "error"}],
    usage=[{"model", "provider", "prompt_tokens", "completion_tokens", "cost", "scope"}].
    """
    def _seed(org_id, report_id, runs):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from app.models.completion import Completion
        from app.models.agent_execution import AgentExecution
        from app.models.tool_execution import ToolExecution
        from app.models.completion_feedback import CompletionFeedback
        from app.models.llm_usage_record import LLMUsageRecord
        from app.models.llm_provider import LLMProvider
        from app.models.llm_model import LLMModel

        url = os.environ["TEST_DATABASE_URL"]
        sync_url = url.replace("sqlite+aiosqlite:", "sqlite:").replace("postgresql+asyncpg:", "postgresql:")
        engine = create_engine(sync_url)
        created_ids = []
        model_cache = {}

        def _model(session, model_id, provider_type):
            key = (org_id, model_id, provider_type)
            if key in model_cache:
                return model_cache[key]
            provider = session.query(LLMProvider).filter_by(
                organization_id=org_id, provider_type=provider_type, name=f"seed-{provider_type}"
            ).first()
            if provider is None:
                provider = LLMProvider(name=f"seed-{provider_type}", provider_type=provider_type,
                                       organization_id=org_id, use_preset_credentials=False)
                session.add(provider)
                session.flush()
            model = session.query(LLMModel).filter_by(
                organization_id=org_id, provider_id=provider.id, model_id=model_id
            ).first()
            if model is None:
                model = LLMModel(name=model_id, model_id=model_id, provider_id=provider.id,
                                 organization_id=org_id, is_custom=True)
                session.add(model)
                session.flush()
            model_cache[key] = model.id
            return model.id

        try:
            with Session(engine) as session:
                for run in runs:
                    created_at = run.get("created_at", datetime.utcnow())
                    judge = run.get("judge") or {}
                    user_completion = Completion(
                        prompt={"content": run.get("prompt", "test prompt")},
                        completion={"content": ""},
                        role="user",
                        message_type="user_message",
                        report_id=report_id,
                        user_id=run.get("user_id"),
                        created_at=created_at,
                        external_platform=run.get("platform"),
                        response_score=judge.get("response", 4),
                        instructions_effectiveness=judge.get("instructions", 4),
                        context_effectiveness=judge.get("context", 4),
                    )
                    session.add(user_completion)
                    session.flush()

                    system_completion = Completion(
                        prompt={"content": ""},
                        completion={"content": "done"},
                        role="system",
                        parent_id=user_completion.id,
                        report_id=report_id,
                        created_at=created_at,
                    )
                    session.add(system_completion)
                    session.flush()

                    duration = run.get("duration_ms")
                    ae = AgentExecution(
                        completion_id=system_completion.id,
                        organization_id=org_id,
                        user_id=run.get("user_id"),
                        report_id=report_id,
                        status=run.get("status", "completed"),
                        created_at=created_at,
                        started_at=created_at,
                        completed_at=(created_at + timedelta(milliseconds=duration)) if duration else None,
                        total_duration_ms=duration,
                        first_token_ms=run.get("first_token_ms"),
                        thinking_ms=run.get("thinking_ms"),
                        error_json={"message": run["error"]} if run.get("error") else None,
                        is_eval_run=bool(run.get("is_eval_run", False)),
                        token_usage_json=run.get("token_usage_json"),
                    )
                    session.add(ae)
                    session.flush()
                    created_ids.append(ae.id)

                    for i, tool in enumerate(run.get("tools") or []):
                        status = tool.get("status", "success")
                        session.add(ToolExecution(
                            agent_execution_id=ae.id,
                            tool_name=tool["name"],
                            tool_action=tool.get("action"),
                            arguments_json={},
                            status=status,
                            success=(status == "success"),
                            started_at=created_at + timedelta(seconds=i),
                            completed_at=created_at + timedelta(seconds=i, milliseconds=tool.get("duration_ms") or 0),
                            duration_ms=tool.get("duration_ms"),
                            attempt_number=tool.get("attempt", 1),
                            max_retries=tool.get("max_retries", 0),
                            error_message=tool.get("error"),
                            created_at=created_at + timedelta(seconds=i),
                        ))

                    fb = run.get("feedback")
                    if fb:
                        session.add(CompletionFeedback(
                            completion_id=system_completion.id,
                            organization_id=org_id,
                            user_id=run.get("user_id"),
                            direction=fb["direction"],
                            message=fb.get("message"),
                            created_at=created_at,
                        ))

                    for u in run.get("usage") or []:
                        model_id = u.get("model", "seed-model")
                        provider_type = u.get("provider", "openai")
                        session.add(LLMUsageRecord(
                            scope=u.get("scope", "planner"),
                            scope_ref_id=None,
                            organization_id=org_id,
                            user_id=run.get("user_id"),
                            report_id=report_id,
                            agent_execution_id=None if u.get("unattributed") else ae.id,
                            llm_model_id=_model(session, model_id, provider_type),
                            model_id=model_id,
                            provider_type=provider_type,
                            prompt_tokens=u.get("prompt_tokens", 0),
                            completion_tokens=u.get("completion_tokens", 0),
                            total_cost_usd=u.get("cost", 0.0),
                            input_cost_usd=u.get("cost", 0.0),
                            output_cost_usd=0.0,
                            created_at=created_at + timedelta(milliseconds=1),
                        ))
                session.commit()
        finally:
            engine.dispose()
        return created_ids

    return _seed


@pytest.fixture
def rollup_agent_executions():
    """Run the diagnosis rollup over every run in the test database — what
    ``finish_agent_execution`` (and the feedback / judge hooks) do for live
    runs, applied to seeded ones."""
    def _rollup(single: bool = False):
        """``single=True`` rolls each run up one at a time through
        ``refresh_rollup`` (the write-path hook); the default uses the batched
        backfill. Both must produce the same columns."""
        import asyncio
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from app.models.agent_execution import AgentExecution
        from app.services.diagnosis.rollup import backfill, refresh_rollup

        async def _run():
            url = os.environ["TEST_DATABASE_URL"]
            if "+" not in url.split("://", 1)[0]:
                url = url.replace("sqlite://", "sqlite+aiosqlite://", 1).replace("postgresql://", "postgresql+asyncpg://", 1)
            engine = create_async_engine(url)
            try:
                async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                    if not single:
                        return await backfill(db, only_missing=False)
                    ids = (await db.execute(select(AgentExecution.id))).scalars().all()
                    for ae_id in ids:
                        await refresh_rollup(db, str(ae_id))
                    return len(ids)
            finally:
                await engine.dispose()

        return asyncio.run(_run())

    return _rollup

@pytest.fixture
def create_test_data_for_console(test_client):
    """Create test data (reports, completions, steps, feedback) for console metrics testing"""
    def _create_test_data_for_console(user_token, org_id):
        # This fixture can be expanded to create test data as needed
        # For now, it's a placeholder for future test data creation
        return {
            "reports_created": 0,
            "completions_created": 0,
            "steps_created": 0,
            "feedbacks_created": 0
        }
    
    return _create_test_data_for_console
