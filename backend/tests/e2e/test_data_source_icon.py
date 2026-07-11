import pytest
from pathlib import Path


DATA_SOURCE_TEST_DB_PATH = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()


@pytest.mark.e2e
def test_data_source_custom_icon_roundtrip(
    create_data_source,
    get_data_source,
    update_data_source,
    test_client,
    create_user,
    login_user,
    whoami,
):
    """A per-agent custom icon override round-trips through update / get / list,
    accepts only namespaced tokens, and can be cleared with an explicit null.

    Regression guard for the custom-agent-icon feature: the icon is a stored
    column, exposed on the data source schemas, settable via the update
    endpoint, and explicit null resets it to the default (None)."""
    if not DATA_SOURCE_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {DATA_SOURCE_TEST_DB_PATH}")

    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {user_token}", "X-Organization-Id": str(org_id)}

    ds = create_data_source(
        name="Icon Agent",
        type="sqlite",
        config={"database": str(DATA_SOURCE_TEST_DB_PATH)},
        credentials={},
        user_token=user_token,
        org_id=org_id,
    )
    ds_id = ds["id"]

    # Default: no custom icon.
    assert ds.get("icon") is None

    # Set an emoji override — it round-trips in the PUT response...
    updated = update_data_source(
        data_source_id=ds_id,
        payload={"icon": "emoji:📊"},
        user_token=user_token,
        org_id=org_id,
    )
    assert updated["icon"] == "emoji:📊"

    # ...and on a fresh GET...
    fetched = get_data_source(data_source_id=ds_id, user_token=user_token, org_id=org_id)
    assert fetched["icon"] == "emoji:📊"

    # ...and in the list endpoint (the agents list / selector payload).
    list_resp = test_client.get("/api/data_sources", headers=headers)
    assert list_resp.status_code == 200, list_resp.json()
    listed = {d["id"]: d for d in list_resp.json()}
    assert listed[ds_id]["icon"] == "emoji:📊"

    # A "preset:" token is accepted (reserved for a future preset gallery).
    updated = update_data_source(
        data_source_id=ds_id,
        payload={"icon": "preset:snowflake"},
        user_token=user_token,
        org_id=org_id,
    )
    assert updated["icon"] == "preset:snowflake"

    # An un-namespaced / garbage token is rejected (422), not silently stored.
    bad = test_client.put(
        f"/api/data_sources/{ds_id}",
        json={"icon": "not-a-token"},
        headers=headers,
    )
    assert bad.status_code == 422, bad.json()
    # ...and the previously stored value is unchanged.
    fetched = get_data_source(data_source_id=ds_id, user_token=user_token, org_id=org_id)
    assert fetched["icon"] == "preset:snowflake"

    # Explicit null clears the override back to the default icon.
    cleared = update_data_source(
        data_source_id=ds_id,
        payload={"icon": None},
        user_token=user_token,
        org_id=org_id,
    )
    assert cleared["icon"] is None
    fetched = get_data_source(data_source_id=ds_id, user_token=user_token, org_id=org_id)
    assert fetched["icon"] is None

    # Omitting icon in a later update leaves it unchanged (set it, then update a
    # different field without icon).
    update_data_source(
        data_source_id=ds_id,
        payload={"icon": "emoji:🤖"},
        user_token=user_token,
        org_id=org_id,
    )
    update_data_source(
        data_source_id=ds_id,
        payload={"description": "still has its icon"},
        user_token=user_token,
        org_id=org_id,
    )
    fetched = get_data_source(data_source_id=ds_id, user_token=user_token, org_id=org_id)
    assert fetched["icon"] == "emoji:🤖"
