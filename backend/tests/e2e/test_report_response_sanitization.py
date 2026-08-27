from pathlib import Path

import pytest

DATA_SOURCE_TEST_DB_PATH = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()

SENSITIVE_DS_KEYS = {"context", "config"}
SENSITIVE_CONN_KEYS = {"config", "credentials"}


def _assert_data_sources_sanitized(report: dict):
    """Report responses must not carry agent-management metadata: no data
    source `context`/`config`, and no connection `config`/`credentials`
    (connection config can hold server URLs, MCP static headers, and
    identity header-injection rules)."""
    for ds in report.get("data_sources", []):
        leaked = SENSITIVE_DS_KEYS & set(ds.keys())
        assert not leaked, f"data source leaks {leaked}: {ds}"
        for conn in ds.get("connections", []):
            leaked = SENSITIVE_CONN_KEYS & set(conn.keys())
            assert not leaked, f"connection leaks {leaked}: {conn}"


@pytest.mark.e2e
def test_report_responses_omit_connection_internals(
    test_client,
    create_data_source,
    create_report,
    get_report,
    get_reports,
    create_user,
    login_user,
    whoami,
):
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    if not DATA_SOURCE_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DATA_SOURCE_TEST_DB_PATH}")

    data_source = create_data_source(
        name="Sanitization DS",
        type="sqlite",
        config={"database": str(DATA_SOURCE_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )

    created = create_report(
        title="Sanitization Report",
        user_token=user_token,
        org_id=org_id,
        data_sources=[data_source["id"]],
    )
    assert created["data_sources"], "data source should be attached"
    _assert_data_sources_sanitized(created)

    fetched = get_report(report_id=created["id"], user_token=user_token, org_id=org_id)
    assert fetched["data_sources"], "data source should survive get_report"
    # Display fields the report UI actually needs must still be present.
    ds = fetched["data_sources"][0]
    assert ds["name"] == "Sanitization DS"
    assert ds.get("type") == "sqlite" or any(
        c.get("type") == "sqlite" for c in ds.get("connections", [])
    )
    _assert_data_sources_sanitized(fetched)

    listed = get_reports(user_token=user_token, org_id=org_id)
    for report in listed.get("reports", listed if isinstance(listed, list) else []):
        _assert_data_sources_sanitized(report)

    # Member-reachable data source list endpoints must not embed connection
    # config either — it belongs on the manage-gated surfaces only.
    headers = {"Authorization": f"Bearer {user_token}", "X-Organization-Id": str(org_id)}
    for path in ("/api/data_sources", "/api/data_sources/active?include_unconnected=true"):
        response = test_client.get(path, headers=headers)
        assert response.status_code == 200, response.json()
        for item in response.json():
            for conn in item.get("connections", []):
                assert not conn.get("config"), f"{path} leaks connection config: {conn}"
                assert "credentials" not in conn


@pytest.mark.e2e
def test_report_create_without_title_succeeds(
    test_client,
    create_user,
    login_user,
    whoami,
):
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id),
    }

    # No `title` key at all — used to 500 in slug generation.
    response = test_client.post("/api/reports", json={"data_sources": []}, headers=headers)
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["slug"]
    assert body["slug"].startswith("untitled")

    # Explicit null title — same path.
    response = test_client.post(
        "/api/reports", json={"title": None, "data_sources": []}, headers=headers
    )
    assert response.status_code == 200, response.json()


@pytest.mark.e2e
def test_report_create_ignores_legacy_widget_key(
    test_client,
    create_user,
    login_user,
    whoami,
):
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]

    headers = {
        "Authorization": f"Bearer {user_token}",
        "X-Organization-Id": str(org_id),
    }

    # Old clients may still send `widget`; it was never used server-side and
    # is no longer a schema field — it must be silently ignored.
    response = test_client.post(
        "/api/reports",
        json={"title": "Widget legacy", "widget": {"new_message": "hi"}, "data_sources": []},
        headers=headers,
    )
    assert response.status_code == 200, response.json()
