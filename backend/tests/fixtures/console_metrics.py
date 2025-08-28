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
    def _get_diagnosis_dashboard_metrics(user_token=None, org_id=None, start_date=None, end_date=None):
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
            "/api/console/diagnosis/metrics",
            headers=headers,
            params=params
        )
        return response
    
    return _get_diagnosis_dashboard_metrics

@pytest.fixture
def get_agent_execution_summaries(test_client):
    def _get_agent_execution_summaries(user_token=None, org_id=None, start_date=None, end_date=None, 
                                      page=1, page_size=20, filter=None):
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
        
        response = test_client.get(
            "/api/console/agent_executions/summaries",
            headers=headers,
            params=params
        )
        return response
    
    return _get_agent_execution_summaries

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
