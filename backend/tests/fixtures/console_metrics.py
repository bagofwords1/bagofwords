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
                                      page=1, page_size=20, filter=None, user_ids=None, prompt_search=None):
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

        response = test_client.get(
            "/api/console/agent_executions/summaries",
            headers=headers,
            params=params
        )
        return response

    return _get_agent_execution_summaries

@pytest.fixture
def get_diagnosis_timeseries(test_client):
    def _get_diagnosis_timeseries(user_token=None, org_id=None, start_date=None, end_date=None, user_ids=None):
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
            "/api/console/diagnosis/timeseries",
            headers=headers,
            params=params
        )
        return response

    return _get_diagnosis_timeseries

@pytest.fixture
def get_diagnosis_users(test_client):
    def _get_diagnosis_users(user_token=None, org_id=None):
        headers = {}
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"
        if org_id:
            headers["X-Organization-Id"] = str(org_id)

        response = test_client.get(
            "/api/console/diagnosis/users",
            headers=headers,
        )
        return response

    return _get_diagnosis_users

@pytest.fixture
def seed_agent_executions():
    """Insert agent executions (each with its user→system completion pair)
    directly into the test database.

    Direct DB writes are a last resort per tests/AGENTS.md — agent executions
    are only ever produced by a live agent run (an LLM boundary that e2e tests
    must not cross), so there is no API surface that can create them.
    """
    def _seed(org_id, report_id, runs):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from app.models.completion import Completion
        from app.models.agent_execution import AgentExecution

        url = os.environ["TEST_DATABASE_URL"]
        sync_url = url.replace("sqlite+aiosqlite:", "sqlite:").replace("postgresql+asyncpg:", "postgresql:")
        engine = create_engine(sync_url)
        created_ids = []
        try:
            with Session(engine) as session:
                for run in runs:
                    created_at = run.get("created_at", datetime.utcnow())
                    user_completion = Completion(
                        prompt={"content": run.get("prompt", "test prompt")},
                        completion={"content": ""},
                        role="user",
                        message_type="user_message",
                        report_id=report_id,
                        user_id=run.get("user_id"),
                        created_at=created_at,
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

                    ae = AgentExecution(
                        completion_id=system_completion.id,
                        organization_id=org_id,
                        user_id=run.get("user_id"),
                        report_id=report_id,
                        status=run.get("status", "completed"),
                        created_at=created_at,
                    )
                    session.add(ae)
                    session.flush()
                    created_ids.append(ae.id)
                session.commit()
        finally:
            engine.dispose()
        return created_ids

    return _seed

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
