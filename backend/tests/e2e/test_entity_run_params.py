"""Running a saved entity with parameter VALUES — the no-LLM "load a saved
query with my values" path.

Contract under test (POST /api/entities/{id}/run):
- `params` present → the entity's SAVED code executes with those values as
  the caller; the response carries that slice in `data` and the resolved
  values in `applied_params`; the shared snapshot is NOT rewritten.
- The same caller + same values are served from the per-user cache (no
  re-execution) until `force_refresh`.
- Unknown names and client values for identity-locked params → 400.
- Identity-source params bind to the caller server-side.
- A run without `params` keeps refreshing the shared snapshot.

Run:
    cd backend && uv run pytest tests/e2e/test_entity_run_params.py -v
"""
import uuid

import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


# Pure-pandas so the entity needs no data source. `run_marker` changes on
# every real execution, which is how a cache hit is told apart from a rerun
# without asserting on internals.
PARAM_CODE = """
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    year = params.get("year")
    rows = [
        {"year": y, "total": t}
        for y, t in ((2021, 10), (2022, 20), (2023, 30))
        if year is None or int(y) == int(year)
    ]
    df = pd.DataFrame(rows)
    df["run_marker"] = pd.Timestamp.now().value
    return df
"""

IDENTITY_CODE = """
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    return pd.DataFrame({"viewer": [params.get("viewer_email")], "year": [params.get("year")]})
"""

YEAR_SPEC = {"name": "year", "type": "number", "label": "Year", "default": None, "required": False, "source": "input"}
VIEWER_SPEC = {"name": "viewer_email", "type": "string", "source": "identity", "identity_binding": "viewer.email"}


def _create_entity(test_client, token, org_id, *, code, parameters, title="Sales by year"):
    resp = test_client.post(
        "/api/entities",
        json={
            "type": "model", "title": title, "slug": f"sales-{uuid.uuid4().hex[:8]}",
            "code": code, "data": {}, "status": "published",
            "parameters": parameters, "data_source_ids": [],
        },
        headers=_hdr(token, org_id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The declaration survives creation and is readable back — a value run
    # is impossible otherwise.
    assert [p["name"] for p in (body["parameters"] or [])] == [p["name"] for p in parameters]
    return body


def _run(test_client, token, org_id, entity_id, body):
    return test_client.post(f"/api/entities/{entity_id}/run", json=body, headers=_hdr(token, org_id))


def _rows(payload):
    return payload["data"]["rows"]


@pytest.fixture
def admin(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return {"token": token, "org_id": org_id}


@pytest.mark.e2e
def test_values_run_returns_that_slice_and_leaves_the_shared_snapshot_alone(test_client, admin):
    ent = _create_entity(test_client, admin["token"], admin["org_id"], code=PARAM_CODE, parameters=[YEAR_SPEC])

    # Refresh the shared snapshot: defaults (year=All) → every row.
    snap = _run(test_client, admin["token"], admin["org_id"], ent["id"], {})
    assert snap.status_code == 200, snap.text
    assert len(_rows(snap.json())) == 3
    assert snap.json()["applied_params"] == {"year": None}

    # A values run answers only those values (typed: "2022" is coerced).
    year = 2022
    run = _run(test_client, admin["token"], admin["org_id"], ent["id"], {"params": {"year": str(year)}})
    assert run.status_code == 200, run.text
    assert [r["year"] for r in _rows(run.json())] == [year]
    assert run.json()["applied_params"]["year"] == year

    # …and the shared snapshot still holds the default (All) slice.
    got = test_client.get(f"/api/entities/{ent['id']}", headers=_hdr(admin["token"], admin["org_id"]))
    assert got.status_code == 200
    assert len(_rows(got.json())) == 3
    assert got.json()["applied_params"] == {"year": None}


@pytest.mark.e2e
def test_repeated_values_run_is_served_from_cache_until_force_refresh(test_client, admin):
    ent = _create_entity(test_client, admin["token"], admin["org_id"], code=PARAM_CODE, parameters=[YEAR_SPEC])

    first = _run(test_client, admin["token"], admin["org_id"], ent["id"], {"params": {"year": 2023}})
    again = _run(test_client, admin["token"], admin["org_id"], ent["id"], {"params": {"year": 2023}})
    assert first.status_code == again.status_code == 200
    marker = lambda r: {row["run_marker"] for row in _rows(r.json())}  # noqa: E731
    assert marker(again) == marker(first), "same values must not re-execute"

    other = _run(test_client, admin["token"], admin["org_id"], ent["id"], {"params": {"year": 2021}})
    assert other.status_code == 200
    assert marker(other) != marker(first), "different values are a different cache slot"
    assert [r["year"] for r in _rows(other.json())] == [2021]

    forced = _run(test_client, admin["token"], admin["org_id"], ent["id"], {"params": {"year": 2023}, "force_refresh": True})
    assert forced.status_code == 200
    assert marker(forced) != marker(first), "force_refresh must re-execute"


@pytest.mark.e2e
def test_values_run_rejects_unknown_and_identity_locked_parameters(test_client, admin):
    ent = _create_entity(test_client, admin["token"], admin["org_id"], code=IDENTITY_CODE, parameters=[YEAR_SPEC, VIEWER_SPEC])

    unknown = _run(test_client, admin["token"], admin["org_id"], ent["id"], {"params": {"quarter": 1}})
    assert unknown.status_code == 400, unknown.text

    locked = _run(test_client, admin["token"], admin["org_id"], ent["id"], {"params": {"viewer_email": "someone@else.example"}})
    assert locked.status_code == 400, locked.text

    no_params = _create_entity(test_client, admin["token"], admin["org_id"], code=PARAM_CODE, parameters=[])
    stray = _run(test_client, admin["token"], admin["org_id"], no_params["id"], {"params": {"year": 2022}})
    assert stray.status_code == 400, stray.text


@pytest.mark.e2e
def test_identity_parameters_bind_to_the_caller_on_values_runs(test_client, admin, create_user, login_user, whoami):
    ent = _create_entity(test_client, admin["token"], admin["org_id"], code=IDENTITY_CODE, parameters=[YEAR_SPEC, VIEWER_SPEC])
    me = whoami(admin["token"])

    run = _run(test_client, admin["token"], admin["org_id"], ent["id"], {"params": {"year": 2022}})
    assert run.status_code == 200, run.text
    rows = _rows(run.json())
    assert rows and rows[0]["viewer"] == me["email"]
    assert run.json()["applied_params"]["viewer_email"] == me["email"]
    assert run.json()["applied_params"]["year"] == 2022
