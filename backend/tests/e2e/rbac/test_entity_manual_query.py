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
def test_manual_create_clamps_status_to_published_or_draft(test_client, world):
    resp = _create(test_client, world, "manager", status="archived")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "draft"
    assert resp.json()["published_at"] is None


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
    r = _create(test_client, world, "member", data_source_ids=[])
    assert r.status_code == 403 and r.json()["error_code"] == "entity.agent_required"
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

PARAM_CODE = """
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    rows = [{"country": "IL", "n": 1}, {"country": "FR", "n": 2}, {"country": "DE", "n": 3}]
    c = params.get("country")
    return pd.DataFrame([r for r in rows if c is None or r["country"] == c])
"""

IDENTITY_CODE = """
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    return pd.DataFrame({"who": [params["who"]]})
"""

COUNTRY_SPEC = {"name": "country", "type": "string", "label": "Country", "options": ["IL", "FR", "DE"]}


@pytest.mark.e2e
def test_preview_declared_params_with_test_values(test_client, world):
    # Nothing declared: the read yields None, the "All" path returns every row.
    resp = _preview(test_client, world, "manager", code=PARAM_CODE)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]["rows"]) == 3
    resp = test_client.post(
        "/api/entities/preview",
        json={"code": PARAM_CODE, "data_source_ids": [world["ds_public"]["id"]],
              "parameters": [COUNTRY_SPEC]},
        headers=_hdr(world["manager"]["token"], world["org_id"]),
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]["rows"]) == 3
    assert resp.json()["applied_params"] == {"country": None}
    # A test value narrows the run.
    resp = test_client.post(
        "/api/entities/preview",
        json={"code": PARAM_CODE, "data_source_ids": [world["ds_public"]["id"]],
              "parameters": [COUNTRY_SPEC], "params": {"country": "FR"}},
        headers=_hdr(world["manager"]["token"], world["org_id"]),
    )
    assert resp.status_code == 200, resp.text
    assert [r["country"] for r in resp.json()["data"]["rows"]] == ["FR"]
    assert resp.json()["applied_params"] == {"country": "FR"}


@pytest.mark.e2e
def test_preview_rejects_inconsistent_or_unknown_params(test_client, world):
    hdr = _hdr(world["manager"]["token"], world["org_id"])
    base = {"code": PARAM_CODE, "data_source_ids": [world["ds_public"]["id"]]}
    # Declared but never read by the code.
    r = test_client.post("/api/entities/preview", json={**base, "parameters": [COUNTRY_SPEC, {"name": "unused", "type": "string"}]}, headers=hdr)
    assert r.status_code == 400 and "unused" in r.json()["detail"]
    # Value for a parameter that is not declared.
    r = test_client.post("/api/entities/preview", json={**base, "parameters": [COUNTRY_SPEC], "params": {"city": "Paris"}}, headers=hdr)
    assert r.status_code == 400 and "city" in r.json()["detail"]
    # Duplicate names.
    r = test_client.post("/api/entities/preview", json={**base, "parameters": [COUNTRY_SPEC, COUNTRY_SPEC]}, headers=hdr)
    assert r.status_code == 400 and r.json()["error_code"] == "entity.params_invalid"
    # The global create shares the validation and the typed error.
    r = test_client.post(
        "/api/entities/global",
        json={"type": "model", "title": "g", "code": PARAM_CODE, "data": {}, "status": "draft",
              "data_source_ids": [world["ds_public"]["id"]], "parameters": [{"name": "unused", "type": "string"}]},
        headers=_hdr(world["admin"]["token"], world["org_id"]),
    )
    assert r.status_code == 400 and r.json()["error_code"] == "entity.params_invalid"


@pytest.mark.e2e
def test_identity_param_binds_to_the_caller(test_client, world):
    spec = {"name": "who", "type": "string", "source": "identity", "identity_binding": "viewer.email"}
    for who in ("manager", "member"):
        resp = test_client.post(
            "/api/entities/preview",
            json={"code": IDENTITY_CODE, "data_source_ids": [world["ds_public"]["id"]], "parameters": [spec]},
            headers=_hdr(world[who]["token"], world["org_id"]),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["rows"][0]["who"] == world[who]["email"]
    # The client may not override an identity-locked value.
    resp = test_client.post(
        "/api/entities/preview",
        json={"code": IDENTITY_CODE, "data_source_ids": [world["ds_public"]["id"]],
              "parameters": [spec], "params": {"who": "someone@else.com"}},
        headers=_hdr(world["member"]["token"], world["org_id"]),
    )
    assert resp.status_code == 400


@pytest.mark.e2e
def test_saved_params_drive_the_query_and_its_options(test_client, world):
    hdr = _hdr(world["manager"]["token"], world["org_id"])
    created = _create(test_client, world, "manager", title="By country", code=PARAM_CODE, parameters=[COUNTRY_SPEC])
    assert created.status_code == 200, created.text
    eid = created.json()["id"]
    assert [p["name"] for p in created.json()["parameters"]] == ["country"]

    # Static choices come back as {value,label} for the control.
    opts = test_client.get(f"/api/entities/{eid}/param-options", headers=hdr)
    assert opts.status_code == 200, opts.text
    assert [o["value"] for o in opts.json()["country"]] == ["IL", "FR", "DE"]

    # A viewer run with a value uses the saved declarations.
    run = test_client.post(f"/api/entities/{eid}/run", json={"params": {"country": "DE"}}, headers=hdr)
    assert run.status_code == 200, run.text
    assert [r["country"] for r in run.json()["data"]["rows"]] == ["DE"]

    # Editing declarations is checked against the stored code...
    bad = test_client.put(f"/api/entities/{eid}", json={"parameters": [{"name": "region", "type": "string"}]}, headers=hdr)
    assert bad.status_code == 400
    # ...and against new code sent together with them.
    ok = test_client.put(
        f"/api/entities/{eid}",
        json={"code": IDENTITY_CODE, "parameters": [{"name": "who", "type": "string", "source": "identity"}]},
        headers=hdr,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["parameters"][0]["identity_binding"] == "viewer.email"


@pytest.mark.e2e
def test_undeclared_reads_are_allowed_consistently(test_client, world):
    """Reading a parameter nobody declared just yields None ("All"), and Save
    agrees with Run about it: both accept, with or without an empty list."""
    hdr = _hdr(world["manager"]["token"], world["org_id"])
    for parameters in ([], None):
        extra = {"parameters": parameters} if parameters is not None else {}
        resp = _create(test_client, world, "manager", code=PARAM_CODE, **extra)
        assert resp.status_code == 200, resp.text
        pv = test_client.post(
            "/api/entities/preview",
            json={"code": PARAM_CODE, "data_source_ids": [world["ds_public"]["id"]], **extra},
            headers=hdr,
        )
        assert pv.status_code == 200, pv.text
        assert len(pv.json()["data"]["rows"]) == 3
    # Once something is declared, the declared-vs-read check applies.
    resp = _create(test_client, world, "manager", code=PARAM_CODE,
                   parameters=[{"name": "other", "type": "string"}])
    assert resp.status_code == 400 and resp.json()["error_code"] == "entity.params_invalid"


@pytest.mark.e2e
def test_admin_approval_keeps_edited_declarations(test_client, world):
    """Approving a suggestion in the same PUT that edits code + declarations
    must persist both — a review that drops the declarations publishes code
    reading a parameter nobody declared."""
    suggested = _create(test_client, world, "member", code=PANDAS_CODE)
    assert suggested.status_code == 200 and suggested.json()["global_status"] == "suggested"
    eid = suggested.json()["id"]
    resp = test_client.put(
        f"/api/entities/{eid}",
        json={"status": "published", "code": PARAM_CODE, "parameters": [COUNTRY_SPEC]},
        headers=_hdr(world["admin"]["token"], world["org_id"]),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["global_status"] == "approved" and body["status"] == "published"
    assert [p["name"] for p in body["parameters"]] == ["country"]


@pytest.mark.e2e
def test_promoted_entity_keeps_its_report_option_source(test_client, world):
    """An entity promoted from a dashboard query carries options_source.query_id;
    editing it must neither refuse nor strip that reference."""
    spec = {"name": "country", "type": "string",
            "options_source": {"query_id": "some-report-query", "value_column": "code"}}
    created = _create(test_client, world, "manager", code=PARAM_CODE, parameters=[spec])
    assert created.status_code == 200, created.text
    eid = created.json()["id"]
    resp = test_client.put(f"/api/entities/{eid}", json={"title": "renamed", "parameters": [spec]},
                           headers=_hdr(world["manager"]["token"], world["org_id"]))
    assert resp.status_code == 200, resp.text
    assert resp.json()["parameters"][0]["options_source"]["query_id"] == "some-report-query"
    # No list can be served for it — the control falls back to free input.
    opts = test_client.get(f"/api/entities/{eid}/param-options", headers=_hdr(world["manager"]["token"], world["org_id"]))
    assert opts.status_code == 200 and "country" not in opts.json()


@pytest.mark.e2e
def test_options_from_another_saved_query(test_client, world):
    hdr = _hdr(world["manager"]["token"], world["org_id"])
    # The filter-space query: a published list of countries with a snapshot.
    countries = _create(
        test_client, world, "manager", title="Countries",
        code="def generate_df(ds_clients, excel_files):\n    import pandas as pd\n    return pd.DataFrame({'code': ['IL', 'FR', 'FR'], 'name': ['Israel', 'France', 'France']})\n",
    )
    assert countries.status_code == 200, countries.text
    cid = countries.json()["id"]
    ran = test_client.post(f"/api/entities/{cid}/run", json={}, headers=hdr)
    assert ran.status_code == 200, ran.text

    spec = {"name": "country", "type": "string",
            "options_source": {"entity_id": cid, "value_column": "code", "label_column": "name"}}
    # A column the source does not have is refused at save time.
    bad = _create(test_client, world, "manager", title="Bad ref", code=PARAM_CODE,
                  parameters=[{**spec, "options_source": {"entity_id": cid, "value_column": "nope"}}])
    assert bad.status_code == 400 and "nope" in bad.json()["detail"]
    created = _create(test_client, world, "manager", title="Sales by country", code=PARAM_CODE, parameters=[spec])
    assert created.status_code == 200, created.text
    eid = created.json()["id"]
    # Distinct values of the source snapshot, labelled — for the creator and a plain reader alike.
    for who in ("manager", "member"):
        opts = test_client.get(f"/api/entities/{eid}/param-options", headers=_hdr(world[who]["token"], world["org_id"]))
        assert opts.status_code == 200, opts.text
        assert opts.json()["country"] == [{"value": "IL", "label": "Israel"}, {"value": "FR", "label": "France"}]
    # A query cannot take its choices from itself.
    self_ref = test_client.put(
        f"/api/entities/{eid}",
        json={"parameters": [{**spec, "options_source": {"entity_id": eid, "value_column": "country"}}]},
        headers=hdr,
    )
    assert self_ref.status_code == 400


@pytest.mark.e2e
def test_param_options_withhold_a_user_scoped_source_from_non_owners(test_client, world):
    """A source snapshot on a user-scoped agent is one identity's slice: its
    values go only to whom GET /entities/{id} would serve the snapshot."""
    import asyncio
    from tests.e2e.rbac.test_rbac_entity_creation import _flip_ds_user_required
    hdr_admin = _hdr(world["admin"]["token"], world["org_id"])
    customers = _create(
        test_client, world, "admin", title="Customers",
        code="def generate_df(ds_clients, excel_files):\n    import pandas as pd\n    return pd.DataFrame({'name': ['Acme', 'Globex']})\n",
    )
    assert customers.status_code == 200, customers.text
    cid = customers.json()["id"]
    assert test_client.post(f"/api/entities/{cid}/run", json={}, headers=hdr_admin).status_code == 200
    asyncio.run(_flip_ds_user_required(world["ds_public"]["id"]))

    spec = {"name": "customer", "type": "string",
            "options_source": {"entity_id": cid, "value_column": "name"}}
    created = _create(test_client, world, "admin", title="Orders", code=PARAM_CODE.replace("country", "customer"), parameters=[spec])
    assert created.status_code == 200, created.text
    eid = created.json()["id"]
    # The owner of the source sees the list; another member does not.
    mine = test_client.get(f"/api/entities/{eid}/param-options", headers=hdr_admin)
    assert mine.status_code == 200 and [o["value"] for o in mine.json()["customer"]] == ["Acme", "Globex"]
    theirs = test_client.get(f"/api/entities/{eid}/param-options", headers=_hdr(world["member"]["token"], world["org_id"]))
    assert theirs.status_code == 200 and "customer" not in theirs.json()
