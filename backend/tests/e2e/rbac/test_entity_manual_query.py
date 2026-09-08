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


# ── parameters ───────────────────────────────────────────────────────────
#
# The form saves ParamSpec declarations with the query and previews with test
# values, so what Run shows is what the saved query will run.

PARAMS_CODE = """
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    rows = [{"country": "Brazil", "v": 1}, {"country": "Canada", "v": 2}]
    country = params.get("country")
    since = params.get("since")
    out = [r for r in rows if country is None or r["country"] == country]
    return pd.DataFrame([{**r, "since": since, "who": params.get("rep_email")} for r in out])
"""

DECLARED = [
    {"name": "since", "type": "date", "source": "input", "default": "2009-01-01"},
    {"name": "country", "type": "string", "source": "input", "default": None},
    {"name": "rep_email", "type": "string", "source": "identity", "identity_binding": "viewer.email"},
]


def _preview_params(test_client, world, who, parameters=DECLARED, params=None, code=PARAMS_CODE):
    body = {"code": code, "data_source_ids": [world["ds_public"]["id"]], "parameters": parameters}
    if params is not None:
        body["params"] = params
    return test_client.post(
        "/api/entities/preview", json=body, headers=_hdr(world[who]["token"], world["org_id"]),
    )


@pytest.mark.e2e
def test_preview_resolves_declared_params_defaults_values_and_identity(test_client, world):
    """Defaults apply, sent test values override them, and an identity param
    binds to the CALLER — the preview never lets a client pick that value."""
    resp = _preview_params(test_client, world, "manager", params={"country": "Canada"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("error") is None, body
    rows = body["data"]["rows"]
    assert [r["country"] for r in rows] == ["Canada"]
    assert rows[0]["since"] == "2009-01-01"
    assert rows[0]["who"] == world["manager"]["email"]
    assert body["applied_params"] == {
        "since": "2009-01-01", "country": "Canada", "rep_email": world["manager"]["email"],
    }

    # No values: the optional param resolves to NULL and the code's "All"
    # branch keeps every row.
    resp = _preview_params(test_client, world, "manager")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]["rows"]) == 2
    assert resp.json()["applied_params"]["country"] is None


@pytest.mark.e2e
def test_preview_rejects_unknown_and_identity_locked_values(test_client, world):
    resp = _preview_params(test_client, world, "manager", params={"nope": 1})
    assert resp.status_code == 400, resp.text
    assert "nope" in resp.json()["detail"]
    resp = _preview_params(test_client, world, "manager", params={"rep_email": "x@y.z"})
    assert resp.status_code == 400, resp.text
    # A code that declares nothing refuses stray values too.
    resp = _preview_params(test_client, world, "manager", parameters=[], params={"country": "Brazil"})
    assert resp.status_code == 400, resp.text


@pytest.mark.e2e
def test_preview_and_create_refuse_a_broken_declaration(test_client, world):
    bad = [{"name": "not a name", "type": "string"}]
    resp = _preview_params(test_client, world, "manager", parameters=bad)
    assert resp.status_code == 400, resp.text
    assert "invalid parameter declaration" in resp.json()["detail"]
    resp = _create(test_client, world, "manager", code=PARAMS_CODE, parameters=bad)
    assert resp.status_code == 400, resp.text
    dup = [{"name": "since", "type": "date"}, {"name": "since", "type": "string"}]
    resp = _create(test_client, world, "manager", code=PARAMS_CODE, parameters=dup)
    assert resp.status_code == 400, resp.text
    assert "duplicate" in resp.json()["detail"]


@pytest.mark.e2e
def test_declarations_are_saved_on_create_and_editable_by_admin_and_owner(test_client, world):
    """Create persists the declarations (normalized), the first run applies
    their defaults, and both an entity manager and a suggesting owner can
    change them through PUT — they travel with the code."""
    resp = _create(test_client, world, "manager", code=PARAMS_CODE, parameters=DECLARED)
    assert resp.status_code == 200, resp.text
    ent = resp.json()
    names = [p["name"] for p in ent["parameters"]]
    assert names == ["since", "country", "rep_email"]
    assert ent["parameters"][2]["identity_binding"] == "viewer.email"

    hdr = _hdr(world["manager"]["token"], world["org_id"])
    run = test_client.post(f"/api/entities/{ent['id']}/run", json={"code": PARAMS_CODE}, headers=hdr)
    assert run.status_code == 200, run.text
    assert run.json()["applied_params"]["since"] == "2009-01-01"
    assert run.json()["applied_params"]["rep_email"] == world["manager"]["email"]
    assert len(run.json()["data"]["rows"]) == 2

    # Edit preview: edited declarations + a test value, before anything is saved.
    pv = test_client.post(
        f"/api/entities/{ent['id']}/preview",
        json={"code": PARAMS_CODE, "parameters": DECLARED[:2], "params": {"country": "Brazil"}},
        headers=hdr,
    )
    assert pv.status_code == 200, pv.text
    assert [r["country"] for r in pv.json()["data"]["rows"]] == ["Brazil"]
    assert "rep_email" not in pv.json()["applied_params"]
    # ...and the saved declarations still apply when none are sent.
    pv = test_client.post(f"/api/entities/{ent['id']}/preview", json={"code": PARAMS_CODE}, headers=hdr)
    assert pv.status_code == 200, pv.text
    assert pv.json()["applied_params"]["rep_email"] == world["manager"]["email"]

    upd = test_client.put(
        f"/api/entities/{ent['id']}", json={"parameters": DECLARED[:1]}, headers=hdr,
    )
    assert upd.status_code == 200, upd.text
    assert [p["name"] for p in upd.json()["parameters"]] == ["since"]

    # A member's suggestion: the owner edits its declarations too.
    resp = _create(test_client, world, "member", code=PARAMS_CODE, parameters=DECLARED[:1])
    assert resp.status_code == 200, resp.text
    sug = resp.json()
    upd = test_client.put(
        f"/api/entities/{sug['id']}", json={"parameters": DECLARED[:2]},
        headers=_hdr(world["member"]["token"], world["org_id"]),
    )
    assert upd.status_code == 200, upd.text
    assert [p["name"] for p in upd.json()["parameters"]] == ["since", "country"]
