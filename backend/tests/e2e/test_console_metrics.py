import pytest
from datetime import datetime, timedelta

@pytest.mark.e2e
def test_console_basic_metrics_access(
    get_console_metrics,
    create_user,
    login_user,
    create_organization
):
    """Test basic access to console metrics endpoint"""
    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = create_organization(user_token=user_token)
    
    # Test basic metrics endpoint
    response = get_console_metrics(user_token=user_token, org_id=org_id)
    
    assert response.status_code == 200
    data = response.json()
    
    # Verify response structure matches SimpleMetrics schema
    required_fields = [
        "total_messages", "total_queries", "total_feedbacks", 
        "accuracy", "instructions_coverage", "instructions_effectiveness",
        "context_effectiveness", "response_quality", "active_users"
    ]
    
    for field in required_fields:
        assert field in data, f"Missing required field: {field}"
    
    # Verify data types
    assert isinstance(data["total_messages"], int)
    assert isinstance(data["total_queries"], int) 
    assert isinstance(data["total_feedbacks"], int)
    assert isinstance(data["active_users"], int)
    assert isinstance(data["accuracy"], str)
    assert isinstance(data["instructions_coverage"], str)
    assert isinstance(data["instructions_effectiveness"], (int, float))
    assert isinstance(data["context_effectiveness"], (int, float))
    assert isinstance(data["response_quality"], (int, float))

@pytest.mark.e2e
def test_console_metrics_with_date_range(
    get_console_metrics,
    create_user,
    login_user,
    create_organization
):
    """Test console metrics with date range filtering"""
    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = create_organization(user_token=user_token)
    
    # Test with specific date range (last 7 days)
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=7)
    
    response = get_console_metrics(
        user_token=user_token, 
        org_id=org_id,
        start_date=start_date,
        end_date=end_date
    )
    
    assert response.status_code == 200
    data = response.json()
    
    # Should return valid metrics even with date filtering
    assert "total_messages" in data
    assert "total_queries" in data

@pytest.mark.e2e
def test_console_metrics_comparison(
    get_console_metrics_comparison,
    create_user,
    login_user,
    create_organization
):
    """Test console metrics comparison endpoint"""
    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = create_organization(user_token=user_token)
    
    response = get_console_metrics_comparison(user_token=user_token, org_id=org_id)
    
    assert response.status_code == 200
    data = response.json()
    
    # Verify comparison structure
    assert "current" in data
    assert "previous" in data
    assert "changes" in data
    assert "period_days" in data
    
    # Verify current and previous have the same structure as SimpleMetrics
    for period in ["current", "previous"]:
        period_data = data[period]
        assert "total_messages" in period_data
        assert "total_queries" in period_data
        assert "accuracy" in period_data

@pytest.mark.e2e
def test_timeseries_metrics(
    get_timeseries_metrics,
    create_user,
    login_user,
    create_organization
):
    """Test timeseries metrics endpoint"""
    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = create_organization(user_token=user_token)
    
    response = get_timeseries_metrics(user_token=user_token, org_id=org_id)
    
    assert response.status_code == 200
    data = response.json()
    
    # Verify timeseries structure
    assert "date_range" in data
    assert "activity_metrics" in data
    assert "performance_metrics" in data
    
    # Verify activity metrics structure
    activity = data["activity_metrics"]
    assert "messages" in activity
    assert "queries" in activity
    assert isinstance(activity["messages"], list)
    assert isinstance(activity["queries"], list)
    
    # Verify performance metrics structure
    performance = data["performance_metrics"]
    expected_performance_fields = [
        "accuracy", "instructions_coverage", "instructions_effectiveness",
        "context_effectiveness", "response_quality", "positive_feedback_rate"
    ]
    for field in expected_performance_fields:
        assert field in performance
        assert isinstance(performance[field], list)

@pytest.mark.e2e
def test_table_usage_metrics(
    get_table_usage_metrics,
    create_user,
    login_user,
    create_organization
):
    """Test table usage metrics endpoint"""
    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = create_organization(user_token=user_token)
    
    response = get_table_usage_metrics(user_token=user_token, org_id=org_id)
    
    assert response.status_code == 200
    data = response.json()
    
    # Verify table usage structure
    assert "top_tables" in data
    assert "total_queries_analyzed" in data
    assert "date_range" in data
    
    assert isinstance(data["top_tables"], list)
    assert isinstance(data["total_queries_analyzed"], int)



@pytest.mark.e2e  
def test_top_users_metrics(
    get_top_users_metrics,
    create_user,
    login_user,
    create_organization
):
    """Test top users metrics endpoint"""
    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = create_organization(user_token=user_token)
    
    response = get_top_users_metrics(user_token=user_token, org_id=org_id)
    
    assert response.status_code == 200
    data = response.json()
    
    # Verify top users structure
    assert "top_users" in data
    assert "total_users_analyzed" in data
    assert "date_range" in data
    
    assert isinstance(data["top_users"], list)
    assert isinstance(data["total_users_analyzed"], int)

@pytest.mark.e2e
def test_recent_negative_feedback(
    get_recent_negative_feedback,
    create_user,
    login_user,
    create_organization
):
    """Test recent negative feedback endpoint"""
    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = create_organization(user_token=user_token)
    
    response = get_recent_negative_feedback(user_token=user_token, org_id=org_id)
    
    assert response.status_code == 200
    data = response.json()
    
    # Verify negative feedback structure
    assert "recent_feedbacks" in data
    assert "total_negative_feedbacks" in data
    assert "date_range" in data
    
    assert isinstance(data["recent_feedbacks"], list)
    assert isinstance(data["total_negative_feedbacks"], int)

@pytest.mark.e2e
def test_unauthorized_access_to_console_metrics(
    get_console_metrics,
    create_user,
    create_organization
):
    """Test that console metrics require authentication"""
    # Try to access without authentication
    response = get_console_metrics()
    
    # Should return 401 or 403 for unauthorized access
    assert response.status_code in [400, 401, 403]



@pytest.mark.e2e
def test_diagnosis_dashboard_metrics(
    get_diagnosis_dashboard_metrics,
    create_user,
    login_user,
    create_organization
):
    """Test new diagnosis dashboard metrics endpoint (used by diagnosis page dashboard cards)"""
    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = create_organization(user_token=user_token)
    
    response = get_diagnosis_dashboard_metrics(user_token=user_token, org_id=org_id)
    
    assert response.status_code == 200
    data = response.json()
    
    # Verify dashboard metrics structure
    assert "failed_queries" in data
    assert "negative_feedback" in data
    assert "code_errors" in data
    assert "total_items" in data
    
    assert isinstance(data["failed_queries"], int)
    assert isinstance(data["negative_feedback"], int)
    assert isinstance(data["code_errors"], int)
    assert isinstance(data["total_items"], int)

@pytest.mark.e2e
def test_agent_execution_summaries(
    get_agent_execution_summaries,
    create_user,
    login_user,
    create_organization
):
    """Test agent execution summaries endpoint (used by diagnosis page table)"""
    # Setup user and organization
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = create_organization(user_token=user_token)
    
    response = get_agent_execution_summaries(user_token=user_token, org_id=org_id)
    
    assert response.status_code == 200
    data = response.json()
    
    # Verify agent execution summaries structure
    assert "items" in data
    assert "total_items" in data
    assert "date_range" in data
    
    assert isinstance(data["items"], list)
    assert isinstance(data["total_items"], int)
    assert "start" in data["date_range"]
    assert "end" in data["date_range"]
    
    # Test with filters
    filters = ["negative_feedback", "code_errors", "failed_queries"]
    for filter_type in filters:
        filtered_response = get_agent_execution_summaries(
            user_token=user_token, 
            org_id=org_id,
            filter=filter_type
        )
        assert filtered_response.status_code == 200
        filtered_data = filtered_response.json()
        assert "items" in filtered_data