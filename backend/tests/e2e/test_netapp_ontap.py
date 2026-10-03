"""Registry-created ONTAP connections retain diagnostic schema metadata."""

import io
import json

import pytest
import requests


@pytest.mark.e2e
def test_netapp_connection_indexes_curated_and_diagnostic_tables(
    create_user,
    login_user,
    whoami,
    create_data_source,
    refresh_schema,
    test_connection,
    delete_data_source,
    monkeypatch,
):
    def send(session, request, **kwargs):
        assert request.method == "GET"
        response = requests.Response()
        response.status_code = 200
        response.raw = io.BytesIO(
            json.dumps(
                {"uuid": "synthetic-cluster", "name": "lab", "version": {"generation": 9, "major": 14, "minor": 1}}
            ).encode()
        )
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    user = create_user()
    token = login_user(user["email"], user["password"])
    org = whoami(token)["organizations"][0]["id"]
    source = create_data_source(
        name="ONTAP RCA",
        type="netapp_ontap",
        config={"url": "https://ontap.example"},
        credentials={"username": "reader", "password": "synthetic-only"},
        user_token=token,
        org_id=org,
    )
    result = test_connection(data_source_id=source["id"], user_token=token, org_id=org)
    assert result["success"]
    tables = refresh_schema(data_source_id=source["id"], user_token=token, org_id=org)
    names = {t["name"] for t in tables}
    assert {"volumes", "volume_metrics", "volume_aggregates"} <= names
    assert any(name.startswith("diag_") for name in names)
    volume = next(t for t in tables if t["name"] == "volumes")
    assert any(c["name"] == "space.used" for c in volume["columns"])
    assert volume["metadata_json"]["netapp"]["endpoint"] == "/api/storage/volumes"
    delete_data_source(data_source_id=source["id"], user_token=token, org_id=org)
