"""Manual "New query" flow on an agent: create an entity directly (no step)
and preview code before any row exists.

Contract:
- ``POST /api/entities`` derives a slug from the title when none is sent and
  keeps slugs unique per org; the row lands as a catalog row on the chosen
  agent (visible in that agent's list, publisher tier).
- ``POST /api/entities/preview`` runs code against the listed agents without
  persisting anything, for holders of per-agent ``create_entities``; anyone
  else is refused, as is empty code.
"""
import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


PANDAS_CODE = """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    return pd.DataFrame({"v": [1, 2, 3]})
"""


@pytest.fixture
def world(test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source, grant_resource):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    ds_public = sqlite_data_source(
        name="pub_agent", user_token=admin["token"], org_id=org_id, is_public=True,
    )
    ds_private = sqlite_data_source(
        name="prv_agent", user_token=admin["token"], org_id=org_id,
    )
    manager = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    grant_resource(
        resource_type="data_source", resource_id=ds_public["id"],
        principal_type="user", principal_id=manager["user_id"],
        permissions=["manage"], user_token=admin["token"], org_id=org_id,
    )
    return {
        "org_id": org_id, "admin": admin, "manager": manager, "member": member,
        "ds_public": ds_public, "ds_private": ds_private,
        "invite": lambda: invite_user_to_org(org_id=org_id, admin_token=admin["token"]),
    }


def _preview(test_client, world, who, code=PANDAS_CODE, ds_ids=None):
    return test_client.post(
        "/api/entities/preview",
        json={"code": code, "data_source_ids": ds_ids if ds_ids is not None else [world["ds_public"]["id"]]},
        headers=_hdr(world[who]["token"], world["org_id"]),
    )


def _create(test_client, world, who, **overrides):
    body = {
        "type": "model", "title": "Monthly Revenue (EU)", "code": PANDAS_CODE,
        "data": {}, "status": "published", "data_source_ids": [world["ds_public"]["id"]],
    }
    body.update(overrides)
    return test_client.post("/api/entities", json=body, headers=_hdr(world[who]["token"], world["org_id"]))


# ── create ───────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_manual_create_lands_in_agent_list_with_derived_slug(test_client, world):
    resp = _create(test_client, world, "manager")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["slug"] == "monthly-revenue-eu"
    assert body["status"] == "published"
    assert body["private_status"] is None
    assert body["global_status"] == "approved"
    assert body["published_at"] is not None
    assert [d["id"] for d in body["data_sources"]] == [world["ds_public"]["id"]]

    # Visible in the agent's Queries panel for the creator AND for a plain
    # member reading a public agent (it is a catalog row, not a draft).
    for who in ("manager", "member"):
        listed = test_client.get(
            "/api/entities", params={"data_source_ids": world["ds_public"]["id"]},
            headers=_hdr(world[who]["token"], world["org_id"]),
        )
        assert listed.status_code == 200, listed.text
        assert body["id"] in {e["id"] for e in listed.json()}, who


@pytest.mark.e2e
def test_manual_create_draft_stays_out_of_catalog(test_client, world):
    resp = _create(test_client, world, "manager", status="draft")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "draft"
    assert body["published_at"] is None

    # The creator sees their draft; a plain member does not.
    mine = test_client.get(
        "/api/entities", params={"data_source_ids": world["ds_public"]["id"]},
        headers=_hdr(world["manager"]["token"], world["org_id"]),
    ).json()
    assert body["id"] in {e["id"] for e in mine}
    theirs = test_client.get(
        "/api/entities", params={"data_source_ids": world["ds_public"]["id"]},
        headers=_hdr(world["member"]["token"], world["org_id"]),
    ).json()
    assert body["id"] not in {e["id"] for e in theirs}


@pytest.mark.e2e
def test_manual_create_slugs_are_unique_per_org(test_client, world):
    slugs = set()
    for _ in range(3):
        resp = _create(test_client, world, "manager", title="Same title")
        assert resp.status_code == 200, resp.text
        slugs.add(resp.json()["slug"])
    assert len(slugs) == 3
    assert all(s == "same-title" or s.startswith("same-title-") for s in slugs)

    # An explicit slug is honored when free.
    resp = _create(test_client, world, "manager", slug="explicit-one")
    assert resp.status_code == 200, resp.text
    assert resp.json()["slug"] == "explicit-one"


@pytest.mark.e2e
def test_member_with_access_creates_a_suggestion_not_a_catalog_row(test_client, world):
    """Same as the report's Save Query: a plain member may SUGGEST on an agent
    they can access; the row waits for review no matter what status they sent."""
    resp = _create(test_client, world, "member", status="published")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["private_status"] == "published"
    assert body["global_status"] == "suggested"
    assert body["status"] == "draft"
    assert body["published_at"] is None

    # The suggester sees their own row; another plain member does not; an
    # admin (reviewer) does.
    def _ids(who):
        r = test_client.get(
            "/api/entities", params={"data_source_ids": world["ds_public"]["id"]},
            headers=_hdr(world[who]["token"], world["org_id"]),
        )
        assert r.status_code == 200, r.text
        return {e["id"] for e in r.json()}
    assert body["id"] in _ids("member")
    assert body["id"] in _ids("admin")
    other = world["invite"]()
    r = test_client.get(
        "/api/entities", params={"data_source_ids": world["ds_public"]["id"]},
        headers=_hdr(other["token"], world["org_id"]),
    )
    assert body["id"] not in {e["id"] for e in r.json()}


@pytest.mark.e2e
def test_member_without_access_cannot_create_or_preview(test_client, world):
    private = [world["ds_private"]["id"]]
    assert _create(test_client, world, "member", data_source_ids=private).status_code == 403
    assert _preview(test_client, world, "member", ds_ids=private).status_code == 403


@pytest.mark.e2e
def test_agentless_query_stays_admin_only(test_client, world):
    assert _create(test_client, world, "member", data_source_ids=[]).status_code == 403
    assert _create(test_client, world, "manager", data_source_ids=[]).status_code == 403
    assert _preview(test_client, world, "member", ds_ids=[]).status_code == 403
    resp = _create(test_client, world, "admin", data_source_ids=[])
    assert resp.status_code == 200, resp.text
    assert resp.json()["global_status"] == "approved"


# ── preview (no entity row) ──────────────────────────────────────────────

@pytest.mark.e2e
def test_preview_runs_code_without_creating_an_entity(test_client, world):
    before = test_client.get(
        "/api/entities", headers=_hdr(world["manager"]["token"], world["org_id"]),
    ).json()

    resp = _preview(test_client, world, "manager")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("error") is None
    assert len(body["data"]["rows"]) == 3
    assert [c["field"] for c in body["data"]["columns"]] == ["v"]

    after = test_client.get(
        "/api/entities", headers=_hdr(world["manager"]["token"], world["org_id"]),
    ).json()
    assert len(after) == len(before)


@pytest.mark.e2e
def test_preview_queries_the_agent_through_its_client_key(test_client, world):
    ds = test_client.get(
        f"/api/data_sources/{world['ds_public']['id']}",
        headers=_hdr(world["admin"]["token"], world["org_id"]),
    ).json()
    key = f"{ds['name']}:{ds['connections'][0]['name']}"
    code = f'''
def generate_df(ds_clients, excel_files):
    return ds_clients["{key}"].execute_query("select 1 as one, 2 as two")
'''
    resp = _preview(test_client, world, "manager", code=code)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("error") is None, body
    assert [c["field"] for c in body["data"]["columns"]] == ["one", "two"]
    assert body["data"]["rows"][0] == {"one": 1, "two": 2}


@pytest.mark.e2e
def test_preview_reports_code_errors_instead_of_500(test_client, world):
    resp = _preview(test_client, world, "manager", code="def generate_df(ds_clients, excel_files):\n    return undefined_name\n")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"] is None
    assert body["error"]


@pytest.mark.e2e
def test_preview_rejects_empty_code(test_client, world):
    resp = _preview(test_client, world, "manager", code="   ")
    assert resp.status_code == 400, resp.text


@pytest.mark.e2e
def test_preview_requires_access_to_every_agent(test_client, world):
    # Plain member: can access the public agent, so may try a query on it.
    assert _preview(test_client, world, "member").status_code == 200
    # ...but not one that also reads the private agent.
    both = [world["ds_public"]["id"], world["ds_private"]["id"]]
    assert _preview(test_client, world, "member", ds_ids=both).status_code == 403
    assert _preview(test_client, world, "manager", ds_ids=both).status_code == 403
    # Org admin passes everywhere.
    assert _preview(test_client, world, "admin", ds_ids=both).status_code == 200
