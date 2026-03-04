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


# =========================================================================
# File-upload DuckDB connection tests
# =========================================================================


@pytest.fixture
def create_file_database(test_client):
    """Call POST /api/connections/create_file_database."""
    def _create(user_token, org_id, expected_status=200):
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }
        response = test_client.post(
            "/api/connections/create_file_database",
            headers=headers,
        )
        assert response.status_code == expected_status, response.json()
        return response.json()

    return _create


@pytest.fixture
def create_data_source_from_file_with_connection(test_client):
    """Call POST /api/files/{file_id}/create_data_source?connection_id=..."""
    def _create(file_id, connection_id, user_token, org_id, expected_status=200):
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }
        response = test_client.post(
            f"/api/files/{file_id}/create_data_source?connection_id={connection_id}",
            headers=headers,
        )
        assert response.status_code == expected_status, response.json()
        return response.json()

    return _create


@pytest.mark.e2e
def test_create_file_database(
    create_file_database,
    create_user,
    login_user,
    whoami,
):
    """POST /connections/create_file_database creates a DuckDB connection."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    result = create_file_database(token, org_id)

    assert result["type"] == "duckdb"
    assert result["name"] == "My Database"
    assert result["is_active"] is True
    assert result["config"]["is_file_upload"] is True


@pytest.mark.e2e
def test_create_file_database_deduplicates_names(
    create_file_database,
    create_user,
    login_user,
    whoami,
):
    """Creating multiple file databases should produce unique names."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    db1 = create_file_database(token, org_id)
    db2 = create_file_database(token, org_id)

    assert db1["name"] == "My Database"
    assert db2["name"] == "My Database 2"


@pytest.mark.e2e
def test_upload_to_existing_connection(
    create_file_database,
    upload_csv_file,
    create_data_source_from_file_with_connection,
    create_user,
    login_user,
    whoami,
):
    """Upload file with connection_id adds to existing connection."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    # 1. Create file database
    conn = create_file_database(token, org_id)
    conn_id = conn["id"]

    # 2. Upload a CSV
    csv_content = b"product,quantity,price\nWidget,10,9.99\nGadget,5,19.99"
    file_result = upload_csv_file(
        user_token=token, org_id=org_id, filename="products.csv", content=csv_content
    )

    # 3. Add to connection
    result = create_data_source_from_file_with_connection(
        file_result["id"], conn_id, token, org_id
    )

    assert result["connection_id"] == conn_id
    assert result["table_name"] == "products"
    assert result["tables_added"] == 1


@pytest.mark.e2e
def test_upload_multiple_files_to_connection(
    create_file_database,
    upload_csv_file,
    create_data_source_from_file_with_connection,
    create_user,
    login_user,
    whoami,
):
    """Multiple uploads accumulate tables in the same connection."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    conn = create_file_database(token, org_id)
    conn_id = conn["id"]

    # Upload first CSV
    file1 = upload_csv_file(
        user_token=token, org_id=org_id, filename="orders.csv",
        content=b"order_id,total\n1,100\n2,200"
    )
    r1 = create_data_source_from_file_with_connection(
        file1["id"], conn_id, token, org_id
    )
    assert r1["connection_id"] == conn_id

    # Upload second CSV
    file2 = upload_csv_file(
        user_token=token, org_id=org_id, filename="customers.csv",
        content=b"customer_id,name\n1,Alice\n2,Bob"
    )
    r2 = create_data_source_from_file_with_connection(
        file2["id"], conn_id, token, org_id
    )
    assert r2["connection_id"] == conn_id
    assert r2["table_name"] == "customers"
