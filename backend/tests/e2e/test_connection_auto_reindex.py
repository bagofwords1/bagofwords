"""
E2E tests for per-connection scheduled auto-reindex.

Covers the enterprise `scheduled_reindex` feature:
  - default schedule fields surfaced on the connection detail payload,
  - PUT is enterprise-gated (402 without the license feature),
  - PUT persists a custom interval / toggle when licensed,
  - invalid intervals are rejected,
  - the sweeper's staleness gate (`_is_due`) and due-selection behaviour.
"""
import pytest
from datetime import datetime, timedelta
from pathlib import Path


CONNECTION_TEST_DB_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite"
).resolve()


def _auth_headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.mark.e2e
def test_auto_reindex_detail_fields_and_ee_gate(
    test_client,
    create_connection,
    get_connection,
    create_user,
    login_user,
    whoami,
    monkeypatch,
):
    """Detail exposes schedule defaults; PUT is enterprise-gated and validated."""
    if not CONNECTION_TEST_DB_PATH.exists():
        pytest.skip(f"SQLite test database missing at {CONNECTION_TEST_DB_PATH}")

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    conn = create_connection(
        name="Auto Reindex Conn",
        type="sqlite",
        config={"database": str(CONNECTION_TEST_DB_PATH)},
        credentials={},
        user_token=token,
        org_id=org_id,
    )
    cid = conn["id"]

    # Defaults: enabled, NULL interval (=> model default cadence).
    detail = get_connection(connection_id=cid, user_token=token, org_id=org_id)
    assert detail["auto_reindex_enabled"] is True
    assert detail["reindex_interval_hours"] is None
    assert detail["next_retry_at"] is None
    assert detail["last_reindex_error"] is None

    import app.ee.license as lic
    headers = _auth_headers(token, org_id)

    # Unlicensed: customizing the schedule is rejected with 402.
    monkeypatch.setattr(lic, "has_feature", lambda feature: False)
    resp = test_client.put(
        f"/api/connections/{cid}",
        json={"reindex_interval_hours": 24},
        headers=headers,
    )
    assert resp.status_code == 402, resp.json()

    # Licensed: persists a custom interval and the toggle.
    monkeypatch.setattr(lic, "has_feature", lambda feature: True)
    resp = test_client.put(
        f"/api/connections/{cid}",
        json={"reindex_interval_hours": 6, "auto_reindex_enabled": False},
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()
    detail = get_connection(connection_id=cid, user_token=token, org_id=org_id)
    assert detail["reindex_interval_hours"] == 6
    assert detail["auto_reindex_enabled"] is False

    # Licensed but invalid interval => 400.
    resp = test_client.put(
        f"/api/connections/{cid}",
        json={"reindex_interval_hours": 999},
        headers=headers,
    )
    assert resp.status_code == 400, resp.json()


@pytest.mark.e2e
def test_sweeper_staleness_gate():
    """`_is_due` honours the per-connection interval; the model default applies
    when no override is set."""
    from app.models.connection import Connection
    from app.services.scheduled_reindex import _is_due

    now = datetime.utcnow()

    # Never synced => always due.
    c = Connection(name="x", type="sqlite", config={})
    c.last_synced_at = None
    assert _is_due(c, now) is True

    # Synced just now with a 6h override => not due.
    c.reindex_interval_hours = 6
    c.last_synced_at = now - timedelta(hours=1)
    assert _is_due(c, now) is False

    # Past the 6h override => due.
    c.last_synced_at = now - timedelta(hours=7)
    assert _is_due(c, now) is True

    # NULL interval falls back to the model default (12h).
    c.reindex_interval_hours = None
    c.last_synced_at = now - timedelta(hours=11)
    assert _is_due(c, now) is False
    c.last_synced_at = now - timedelta(hours=13)
    assert _is_due(c, now) is True


@pytest.mark.e2e
def test_effective_interval_default():
    """`effective_reindex_interval_hours` resolves override vs default."""
    from app.models.connection import Connection

    c = Connection(name="x", type="sqlite", config={})
    assert c.effective_reindex_interval_hours == Connection.DEFAULT_REINDEX_INTERVAL_HOURS
    c.reindex_interval_hours = 0  # non-positive => default
    assert c.effective_reindex_interval_hours == Connection.DEFAULT_REINDEX_INTERVAL_HOURS
    c.reindex_interval_hours = 8
    assert c.effective_reindex_interval_hours == 8
