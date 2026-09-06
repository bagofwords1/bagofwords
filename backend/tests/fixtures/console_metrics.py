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
    def _get_diagnosis_dashboard_metrics(user_token=None, org_id=None, start_date=None, end_date=None, user_ids=None,
                                         tool_names=None, tool_failed_only=None, table_ids=None, prompt_search=None):
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
        if tool_names:
            params["tool_names"] = tool_names
        if tool_failed_only is not None:
            params["tool_failed_only"] = tool_failed_only
        if table_ids:
            params["table_ids"] = table_ids
        if prompt_search:
            params["prompt_search"] = prompt_search

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
                                      data_source_ids=None, tool_names=None, tool_failed_only=None,
                                      table_ids=None):
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
        if tool_names:
            params["tool_names"] = tool_names
        if tool_failed_only is not None:
            params["tool_failed_only"] = tool_failed_only
        if table_ids:
            params["table_ids"] = table_ids

        response = test_client.get(
            "/api/console/agent_executions/summaries",
            headers=headers,
            params=params
        )
        return response

    return _get_agent_execution_summaries

@pytest.fixture
def get_diagnosis_timeseries(test_client):
    def _get_diagnosis_timeseries(user_token=None, org_id=None, start_date=None, end_date=None, user_ids=None,
                                  tool_names=None, tool_failed_only=None, table_ids=None, prompt_search=None):
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
        if tool_names:
            params["tool_names"] = tool_names
        if tool_failed_only is not None:
            params["tool_failed_only"] = tool_failed_only
        if table_ids:
            params["table_ids"] = table_ids
        if prompt_search:
            params["prompt_search"] = prompt_search

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
def get_diagnosis_tools(test_client):
    def _get_diagnosis_tools(user_token=None, org_id=None):
        headers = {}
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"
        if org_id:
            headers["X-Organization-Id"] = str(org_id)

        response = test_client.get(
            "/api/console/diagnosis/tools",
            headers=headers,
        )
        return response

    return _get_diagnosis_tools

@pytest.fixture
def get_diagnosis_tables(test_client):
    def _get_diagnosis_tables(user_token=None, org_id=None):
        headers = {}
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"
        if org_id:
            headers["X-Organization-Id"] = str(org_id)

        response = test_client.get(
            "/api/console/diagnosis/tables",
            headers=headers,
        )
        return response

    return _get_diagnosis_tables

@pytest.fixture
def get_diagnosis_errors(test_client):
    def _get_diagnosis_errors(user_token=None, org_id=None, **params):
        headers = {}
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"
        if org_id:
            headers["X-Organization-Id"] = str(org_id)

        response = test_client.get(
            "/api/console/diagnosis/errors",
            headers=headers,
            params={k: v for k, v in params.items() if v is not None},
        )
        return response

    return _get_diagnosis_errors

@pytest.fixture
def seed_agent_executions():
    """Insert agent executions (each with its user→system completion pair)
    directly into the test database.

    Direct DB writes are a last resort per tests/AGENTS.md — agent executions
    are only ever produced by a live agent run (an LLM boundary that e2e tests
    must not cross), so there is no API surface that can create them.
    """
    def _seed(org_id, report_id, runs):
        """Each run may carry ``tools``: a list of ``{name, success, table_id?,
        table_fqn?}`` dicts. Every entry becomes a ToolExecution; one with a
        ``table_id`` also gets a Widget+Step and a TableUsageEvent pointing at
        that datasource table, mirroring how a real create_data call records
        table lineage (ToolExecution.created_step_id → TableUsageEvent.step_id).
        """
        import uuid as _uuid
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from app.models.completion import Completion
        from app.models.agent_execution import AgentExecution
        from app.models.tool_execution import ToolExecution
        from app.models.widget import Widget
        from app.models.step import Step
        from app.models.table_usage_event import TableUsageEvent

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

                    for tool in run.get("tools", []):
                        success = bool(tool.get("success", True))
                        step_id = None
                        if tool.get("table_id"):
                            widget = Widget(
                                title="seeded widget",
                                slug=f"seed-w-{_uuid.uuid4().hex}",
                                report_id=report_id,
                            )
                            session.add(widget)
                            session.flush()
                            step = Step(
                                title=tool.get("step_title", "Seeded step"),
                                slug=f"seed-s-{_uuid.uuid4().hex}",
                                widget_id=widget.id,
                                status="success" if success else "error",
                                created_at=created_at,
                            )
                            session.add(step)
                            session.flush()
                            step_id = step.id
                            session.add(TableUsageEvent(
                                org_id=org_id,
                                report_id=report_id,
                                step_id=step_id,
                                user_id=run.get("user_id"),
                                table_fqn=tool.get("table_fqn", "seeded_table"),
                                datasource_table_id=tool["table_id"],
                                source_type="sql",
                                success=success,
                                used_at=created_at,
                            ))
                        session.add(ToolExecution(
                            agent_execution_id=ae.id,
                            tool_name=tool["name"],
                            status="success" if success else "error",
                            success=success,
                            created_step_id=step_id,
                            arguments_json={},
                            duration_ms=tool.get("duration_ms"),
                            error_message=tool.get("error"),
                            created_at=created_at,
                        ))
                session.commit()
        finally:
            engine.dispose()
        return created_ids

    return _seed

@pytest.fixture
def seed_data_table():
    """Insert a DataSource + DataSourceTable pair directly into the test
    database, for exercising the diagnosis table facet/filter. Direct DB
    writes for the same reason as seed_agent_executions: creating a real
    data source and indexing its schema crosses a live-connection boundary
    e2e tests must not cross.

    Returns {"data_source_id", "table_id", "name"}.
    """
    def _seed(org_id, table_name, data_source_name=None):
        import uuid as _uuid
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from app.models.data_source import DataSource
        from app.models.datasource_table import DataSourceTable

        url = os.environ["TEST_DATABASE_URL"]
        sync_url = url.replace("sqlite+aiosqlite:", "sqlite:").replace("postgresql+asyncpg:", "postgresql:")
        engine = create_engine(sync_url)
        try:
            with Session(engine) as session:
                ds = DataSource(
                    name=data_source_name or f"seeded_ds_{_uuid.uuid4().hex[:6]}",
                    organization_id=org_id,
                )
                session.add(ds)
                session.flush()
                table = DataSourceTable(name=table_name, datasource_id=ds.id)
                session.add(table)
                session.flush()
                result = {"data_source_id": str(ds.id), "table_id": str(table.id), "name": table_name}
                session.commit()
        finally:
            engine.dispose()
        return result

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
