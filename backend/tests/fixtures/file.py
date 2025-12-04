import pytest
import io
import os
from pathlib import Path


@pytest.fixture
def upload_file(test_client):
    """Upload a file and return the file metadata."""
    def _upload_file(file_content, filename, content_type, user_token=None, org_id=None, report_id=None):
        if user_token is None:
            pytest.fail("User token is required for upload_file")
        if org_id is None:
            pytest.fail("Organization ID is required for upload_file")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        # Create file-like object
        files = {"file": (filename, file_content, content_type)}
        data = {}
        if report_id:
            data["report_id"] = report_id
        
        response = test_client.post(
            "/api/files",
            files=files,
            data=data if data else None,
            headers=headers
        )
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _upload_file


@pytest.fixture
def upload_csv_file(upload_file):
    """Upload a CSV file with optional custom content."""
    def _upload_csv_file(user_token=None, org_id=None, filename="test_data.csv", content=None, report_id=None):
        if content is None:
            content = b"name,age,city,salary\nAlice,30,NYC,50000\nBob,25,LA,60000\nCharlie,35,Chicago,70000\nDiana,28,Miami,55000\nEve,32,Seattle,65000"
        
        return upload_file(
            file_content=content,
            filename=filename,
            content_type="text/csv",
            user_token=user_token,
            org_id=org_id,
            report_id=report_id
        )
    
    return _upload_csv_file


@pytest.fixture
def upload_excel_file(upload_file):
    """Upload an Excel file. Uses a simple xlsx created in-memory."""
    def _upload_excel_file(user_token=None, org_id=None, filename="test_data.xlsx", sheets=None, report_id=None):
        import pandas as pd
        from io import BytesIO
        
        # Default sheets if none provided
        if sheets is None:
            sheets = {
                "Sales": pd.DataFrame({
                    "Month": ["Jan", "Feb", "Mar", "Apr"],
                    "Revenue": [10000, 12000, 15000, 13000],
                    "Units": [100, 120, 150, 130]
                }),
                "Expenses": pd.DataFrame({
                    "Category": ["Rent", "Salaries", "Marketing", "Utilities"],
                    "Amount": [5000, 20000, 3000, 1000]
                })
            }
        
        # Create Excel file in memory
        buffer = BytesIO()
        with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
            for sheet_name, df in sheets.items():
                df.to_excel(writer, sheet_name=sheet_name, index=False)
        buffer.seek(0)
        
        return upload_file(
            file_content=buffer.getvalue(),
            filename=filename,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            user_token=user_token,
            org_id=org_id,
            report_id=report_id
        )
    
    return _upload_excel_file


@pytest.fixture
def get_files(test_client):
    """Get all files for the organization."""
    def _get_files(user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for get_files")
        if org_id is None:
            pytest.fail("Organization ID is required for get_files")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.get(
            "/api/files",
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _get_files


@pytest.fixture
def get_files_by_report(test_client):
    """Get files associated with a specific report."""
    def _get_files_by_report(report_id, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for get_files_by_report")
        if org_id is None:
            pytest.fail("Organization ID is required for get_files_by_report")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.get(
            f"/api/reports/{report_id}/files",
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _get_files_by_report


@pytest.fixture
def remove_file_from_report(test_client):
    """Remove a file from a report."""
    def _remove_file_from_report(report_id, file_id, user_token=None, org_id=None):
        if user_token is None:
            pytest.fail("User token is required for remove_file_from_report")
        if org_id is None:
            pytest.fail("Organization ID is required for remove_file_from_report")
        
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id)
        }
        
        response = test_client.delete(
            f"/api/reports/{report_id}/files/{file_id}",
            headers=headers
        )
        
        assert response.status_code == 200, response.json()
        return response.json()
    
    return _remove_file_from_report

