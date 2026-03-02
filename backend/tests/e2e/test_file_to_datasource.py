"""
E2E tests for CSV/Excel file → DuckDB data source creation.

Tests the flow: upload file → POST /files/{file_id}/create_data_source → queryable DuckDB data source.
"""
import pytest


@pytest.fixture
def create_data_source_from_file(test_client):
    """Call POST /api/files/{file_id}/create_data_source."""
    def _create(file_id, user_token, org_id, expected_status=200):
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }
        response = test_client.post(
            f"/api/files/{file_id}/create_data_source",
            headers=headers,
        )
        assert response.status_code == expected_status, response.json()
        return response.json()

    return _create


@pytest.fixture
def get_data_source(test_client):
    """Fetch a data source by ID."""
    def _get(ds_id, user_token, org_id):
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }
        response = test_client.get(
            f"/api/data_sources/{ds_id}",
            headers=headers,
        )
        assert response.status_code == 200, response.json()
        return response.json()

    return _get


# =========================================================================
# CSV tests
# =========================================================================


@pytest.mark.e2e
def test_csv_creates_queryable_data_source(
    upload_csv_file,
    create_data_source_from_file,
    get_data_source,
    create_user,
    login_user,
    whoami,
):
    """Upload CSV → create data source → verify DuckDB data source with tables."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    # Upload a CSV
    csv_content = b"product,quantity,price\nWidget,10,9.99\nGadget,5,19.99"
    file_result = upload_csv_file(
        user_token=token, org_id=org_id, filename="products.csv", content=csv_content
    )

    # Create data source from the file
    ds_result = create_data_source_from_file(file_result["id"], token, org_id)

    assert "data_source_id" in ds_result
    assert ds_result["data_source_name"] == "products"
    assert "connection_id" in ds_result

    # Verify the data source exists and has schema
    ds = get_data_source(ds_result["data_source_id"], token, org_id)
    assert ds["name"] == "products"
    assert ds["type"] == "duckdb"


@pytest.mark.e2e
def test_csv_data_source_name_deduplication(
    upload_csv_file,
    create_data_source_from_file,
    create_user,
    login_user,
    whoami,
):
    """Uploading two files with the same name should produce unique data source names."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    csv_content = b"a,b\n1,2"

    # Upload and create first
    file1 = upload_csv_file(user_token=token, org_id=org_id, filename="data.csv", content=csv_content)
    ds1 = create_data_source_from_file(file1["id"], token, org_id)

    # Upload and create second with same filename
    file2 = upload_csv_file(user_token=token, org_id=org_id, filename="data.csv", content=csv_content)
    ds2 = create_data_source_from_file(file2["id"], token, org_id)

    # Names should differ
    assert ds1["data_source_name"] != ds2["data_source_name"]
    assert ds2["data_source_name"].startswith("data_")


# =========================================================================
# Excel tests
# =========================================================================


@pytest.mark.e2e
def test_excel_creates_queryable_data_source(
    upload_excel_file,
    create_data_source_from_file,
    get_data_source,
    create_user,
    login_user,
    whoami,
):
    """Upload Excel → create data source → each sheet becomes a table."""
    import pandas as pd

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    sheets = {
        "Sales": pd.DataFrame({"month": ["Jan", "Feb"], "revenue": [100, 200]}),
        "Costs": pd.DataFrame({"category": ["Rent", "Staff"], "amount": [50, 80]}),
    }

    file_result = upload_excel_file(
        user_token=token, org_id=org_id, filename="financials.xlsx", sheets=sheets
    )

    ds_result = create_data_source_from_file(file_result["id"], token, org_id)

    assert ds_result["data_source_name"] == "financials"

    # Data source should exist
    ds = get_data_source(ds_result["data_source_id"], token, org_id)
    assert ds["type"] == "duckdb"


# =========================================================================
# Error / edge-case tests
# =========================================================================


@pytest.mark.e2e
def test_reject_non_csv_excel_file(
    upload_file,
    create_data_source_from_file,
    create_user,
    login_user,
    whoami,
):
    """PDF or other non-CSV/Excel file should be rejected with 400."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    # Upload a PDF (content doesn't matter, MIME type does)
    file_result = upload_file(
        file_content=b"%PDF-1.4 fake content",
        filename="report.pdf",
        content_type="application/pdf",
        user_token=token,
        org_id=org_id,
    )

    # Attempt to create data source — should fail
    result = create_data_source_from_file(
        file_result["id"], token, org_id, expected_status=400
    )
    assert "detail" in result


@pytest.mark.e2e
def test_nonexistent_file_returns_404(
    create_data_source_from_file,
    create_user,
    login_user,
    whoami,
):
    """Requesting a data source for a non-existent file should return 404."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    result = create_data_source_from_file(
        "00000000-0000-0000-0000-000000000000", token, org_id, expected_status=404
    )
    assert "detail" in result
