"""
E2E tests for file upload and preview functionality.

Tests the new raw preview system where file uploads generate structured previews
(without LLM) that can be used for on-demand analysis by the coder.
"""
import pytest


@pytest.mark.e2e
def test_upload_csv_file_generates_preview(
    upload_csv_file,
    get_files,
    create_user,
    login_user,
    whoami
):
    """Test that uploading a CSV file generates a raw preview."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    
    # Upload CSV file
    file_result = upload_csv_file(user_token=user_token, org_id=org_id)
    
    assert file_result is not None
    assert file_result["filename"] == "test_data.csv"
    assert file_result["content_type"] == "text/csv"
    assert "id" in file_result
    assert "path" in file_result
    
    # Verify file appears in list
    files = get_files(user_token=user_token, org_id=org_id)
    assert len(files) >= 1
    
    # Find the uploaded file
    uploaded = next((f for f in files if f["id"] == file_result["id"]), None)
    assert uploaded is not None


@pytest.mark.e2e
def test_upload_excel_file_generates_preview(
    upload_excel_file,
    get_files,
    create_user,
    login_user,
    whoami
):
    """Test that uploading an Excel file generates a raw preview with sheet info."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    
    # Upload Excel file
    file_result = upload_excel_file(user_token=user_token, org_id=org_id)
    
    assert file_result is not None
    assert file_result["filename"] == "test_data.xlsx"
    assert file_result["content_type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert "id" in file_result
    assert "path" in file_result


@pytest.mark.e2e
def test_upload_csv_with_custom_content(
    upload_csv_file,
    create_user,
    login_user,
    whoami
):
    """Test uploading a CSV with custom content."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    
    # Custom CSV content
    custom_csv = b"product,quantity,price\nWidget A,100,9.99\nWidget B,50,19.99\nWidget C,200,4.99"
    
    file_result = upload_csv_file(
        user_token=user_token,
        org_id=org_id,
        filename="products.csv",
        content=custom_csv
    )
    
    assert file_result is not None
    assert file_result["filename"] == "products.csv"


@pytest.mark.e2e
def test_upload_excel_with_multiple_sheets(
    upload_excel_file,
    create_user,
    login_user,
    whoami
):
    """Test uploading an Excel file with multiple sheets."""
    import pandas as pd
    
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    
    # Create multi-sheet Excel
    sheets = {
        "Q1_Sales": pd.DataFrame({
            "Product": ["A", "B", "C"],
            "Revenue": [1000, 2000, 3000]
        }),
        "Q2_Sales": pd.DataFrame({
            "Product": ["A", "B", "C"],
            "Revenue": [1500, 2500, 3500]
        }),
        "Q3_Sales": pd.DataFrame({
            "Product": ["A", "B", "C"],
            "Revenue": [2000, 3000, 4000]
        }),
        "Summary": pd.DataFrame({
            "Quarter": ["Q1", "Q2", "Q3"],
            "Total": [6000, 7500, 9000]
        })
    }
    
    file_result = upload_excel_file(
        user_token=user_token,
        org_id=org_id,
        filename="quarterly_sales.xlsx",
        sheets=sheets
    )
    
    assert file_result is not None
    assert file_result["filename"] == "quarterly_sales.xlsx"


@pytest.mark.e2e
def test_upload_file_to_report(
    upload_csv_file,
    create_report,
    get_files_by_report,
    create_user,
    login_user,
    whoami
):
    """Test uploading a file associated with a report."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    
    # Create a report first
    report = create_report(
        title="Sales Analysis Report",
        user_token=user_token,
        org_id=org_id
    )
    report_id = report["id"]
    
    # Upload file to the report
    file_result = upload_csv_file(
        user_token=user_token,
        org_id=org_id,
        report_id=report_id,
        filename="report_data.csv"
    )
    
    assert file_result is not None
    
    # Verify file is associated with report
    report_files = get_files_by_report(
        report_id=report_id,
        user_token=user_token,
        org_id=org_id
    )
    
    assert len(report_files) >= 1
    assert any(f["id"] == file_result["id"] for f in report_files)


@pytest.mark.e2e
def test_remove_file_from_report(
    upload_csv_file,
    create_report,
    get_files_by_report,
    remove_file_from_report,
    create_user,
    login_user,
    whoami
):
    """Test removing a file from a report."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    
    # Create a report
    report = create_report(
        title="Test Report",
        user_token=user_token,
        org_id=org_id
    )
    report_id = report["id"]
    
    # Upload file to report
    file_result = upload_csv_file(
        user_token=user_token,
        org_id=org_id,
        report_id=report_id
    )
    file_id = file_result["id"]
    
    # Verify file is associated
    files_before = get_files_by_report(
        report_id=report_id,
        user_token=user_token,
        org_id=org_id
    )
    assert any(f["id"] == file_id for f in files_before)
    
    # Remove file from report
    remove_result = remove_file_from_report(
        report_id=report_id,
        file_id=file_id,
        user_token=user_token,
        org_id=org_id
    )
    
    assert remove_result is not None
    
    # Verify file is no longer associated
    files_after = get_files_by_report(
        report_id=report_id,
        user_token=user_token,
        org_id=org_id
    )
    assert not any(f["id"] == file_id for f in files_after)


@pytest.mark.e2e
def test_upload_multiple_files(
    upload_csv_file,
    upload_excel_file,
    get_files,
    create_user,
    login_user,
    whoami
):
    """Test uploading multiple files of different types."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    
    # Upload multiple files
    csv_file = upload_csv_file(
        user_token=user_token,
        org_id=org_id,
        filename="data1.csv"
    )
    
    excel_file = upload_excel_file(
        user_token=user_token,
        org_id=org_id,
        filename="data2.xlsx"
    )
    
    csv_file2 = upload_csv_file(
        user_token=user_token,
        org_id=org_id,
        filename="data3.csv",
        content=b"x,y,z\n1,2,3\n4,5,6"
    )
    
    # Verify all files are in the list
    files = get_files(user_token=user_token, org_id=org_id)
    
    file_ids = [f["id"] for f in files]
    assert csv_file["id"] in file_ids
    assert excel_file["id"] in file_ids
    assert csv_file2["id"] in file_ids


@pytest.mark.e2e
def test_upload_large_csv_generates_truncated_preview(
    upload_csv_file,
    create_user,
    login_user,
    whoami
):
    """Test that large CSV files generate a truncated preview (50 rows max)."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    
    # Generate a CSV with 100 rows
    lines = ["id,value,category"]
    for i in range(100):
        lines.append(f"{i},{i*10},cat_{i % 5}")
    large_csv = "\n".join(lines).encode()
    
    file_result = upload_csv_file(
        user_token=user_token,
        org_id=org_id,
        filename="large_data.csv",
        content=large_csv
    )
    
    assert file_result is not None
    assert file_result["filename"] == "large_data.csv"


@pytest.mark.e2e  
def test_upload_excel_with_large_sheet(
    upload_excel_file,
    create_user,
    login_user,
    whoami
):
    """Test that Excel files with large sheets generate truncated previews."""
    import pandas as pd
    
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    
    # Create Excel with large sheet (100 rows)
    sheets = {
        "LargeSheet": pd.DataFrame({
            "id": range(100),
            "value": [i * 10 for i in range(100)],
            "category": [f"cat_{i % 5}" for i in range(100)]
        })
    }
    
    file_result = upload_excel_file(
        user_token=user_token,
        org_id=org_id,
        filename="large_excel.xlsx",
        sheets=sheets
    )
    
    assert file_result is not None
    assert file_result["filename"] == "large_excel.xlsx"



