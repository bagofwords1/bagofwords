"""Connection policy management is enterprise-only and separate from agent table selection."""

import json
import os
from types import SimpleNamespace
from sqlalchemy import create_engine, update
from app.models.user import User

import pytest


@pytest.fixture
def actors(test_client, create_user, login_user, whoami):
    def signup(email):
        password = "LocalFixture123!"
        create_user(name=email.split("@")[0], email=email, password=password)
        token = login_user(email, password)
        me = whoami(token)
        return SimpleNamespace(email=email, token=token, user_id=me["id"], info=me)

    admin = signup("policy.admin@example.com")
    org = admin.info["organizations"][0]["id"]

    def headers(actor):
        return {"Authorization": "Bearer " + actor.token, "X-Organization-Id": org}

    resp = test_client.post(
        f"/api/organizations/{org}/members",
        headers=headers(admin),
        json={"email": "policy.member@example.com", "organization_id": org, "role": "member"},
    )
    assert resp.status_code == 200, resp.text
    member = signup("policy.member@example.com")
    member.membership_id = resp.json()["id"]
    return SimpleNamespace(admin=admin, member=member, org=org, headers=headers)


@pytest.fixture
def access_connection(actors, create_connection, request):
    kind = getattr(request, "param", "postgresql")
    key = "BOW_ACCESS_" + kind.upper()
    if kind != "postgresql" and not os.getenv(key):
        pytest.skip(f"Dedicated {kind} source required")
    admin = actors.admin
    cfg = json.loads(os.getenv(key, '{"host":"localhost","port":5432,"database":"postgres","user":"demo"}'))
    params = dict(cfg)
    creds = {k: params.pop(k) for k in ("user", "password") if k in params}
    conn = create_connection(
        name="Access policy sandbox",
        type=("MSSQL" if kind == "mssql" else kind),
        config=params,
        credentials=creds,
        user_token=admin.token,
        org_id=actors.org,
    )
    return "/api/connections/" + conn["id"] + "/company-policy", actors.headers(admin)


def test_draft_and_coverage_require_enterprise_and_management(test_client, access_connection, actors, monkeypatch):
    root, headers = access_connection
    from app.services import custom_query_service

    monkeypatch.setattr(custom_query_service, "has_feature", lambda key: False)
    assert test_client.get(root + "/draft", headers=headers).status_code == 402
    assert test_client.get(root + "/coverage", headers=headers).status_code == 402
    monkeypatch.setattr(custom_query_service, "has_feature", lambda key: True)
    draft = dict(
        reader_source="verified_email",
        tables=[
            dict(
                table="public.sales",
                company_column="company",
                row_attribute="custom.regions",
                columns={"company": None},
            )
        ],
    )
    assert test_client.put(root + "/draft", headers=headers, json=draft).status_code == 200
    assert test_client.get(root + "/coverage", headers=headers).json()["active"] is False
    assert test_client.put(root + "/draft", headers=actors.headers(actors.member), json=draft).status_code in (403, 404)


@pytest.mark.parametrize("access_connection", ["postgresql", "mssql", "oracledb"], indirect=True)
@pytest.mark.skipif(not os.getenv("BOW_ACCESS_POSTGRESQL"), reason="requires seeded disposable SQL fixtures")
def test_live_preview_and_activation(test_client, access_connection, actors, monkeypatch, request):
    from app.services import custom_query_service
    from app.settings.config import settings

    monkeypatch.setattr(custom_query_service, "has_feature", lambda key: True)
    monkeypatch.setattr(settings.bow_config.features, "fabric_company_rls_activation", True)
    root, headers = access_connection
    admin = actors.admin
    # The external IdP's verification is outside this fixture. Mark the synthetic signed-in accounts verified.
    member = actors.member
    engine = create_engine(
        os.environ["TEST_DATABASE_URL"]
        .replace("sqlite+aiosqlite:", "sqlite:")
        .replace("postgresql+asyncpg:", "postgresql:")
    )
    with engine.begin() as conn:
        conn.execute(update(User).where(User.id.in_([admin.user_id, member.user_id])).values(is_verified=True))
    engine.dispose()
    url = f"/api/organizations/{actors.org}/members/{member.membership_id}"
    res = test_client.put(url, headers=headers, json={"custom_attributes": {"regions": ["101", "202"]}})
    assert res.status_code == 200, res.text
    kind = request.node.callspec.params["access_connection"]
    table = os.environ[f"BOW_ACCESS_{kind.upper()}_TABLE"]
    col = str.upper if kind == "oracledb" else str.lower
    data = dict(
        reader_source="verified_email",
        tables=[
            dict(
                table=table,
                company_column=col("company"),
                row_attribute="custom.regions",
                columns={col("company"): None, col("amount"): [admin.email]},
            )
        ],
    )
    assert test_client.put(root + "/draft", headers=headers, json=data).status_code == 200

    def preview(email):
        response = test_client.get(
            root + "/draft/preview", headers=headers, params={"table": table, "member_email": email}
        )
        assert response.status_code == 200, response.text
        return response.json()

    sample = preview(member.email)
    assert sample["columns"] == [col("company")] and sorted(sample["data"]) == [["101"], ["202"]]
    assert preview(admin.email)["access_status"] == "denied"
    response = test_client.post(root + "/activate", headers=headers, json=data)
    assert response.status_code == 204, response.text
    assert test_client.get(root + "/coverage", headers=headers).json()["active"] is True
    assert test_client.put(url, headers=headers, json={"custom_attributes": {}}).status_code == 200
    assert preview(member.email)["access_status"] == "denied"
    history = test_client.get(root.replace("/company-policy", "/queries"), headers=headers)
    assert history.status_code == 200, history.text
    entries = history.json()["items"]
    assert entries and all(e["policy_version"] for e in entries)
    assert any(e["protected_statement"] and member.email not in e["protected_statement"] for e in entries)
    # Enforcement survives expiry; license checks still guard management and history.
    monkeypatch.setattr(custom_query_service, "has_feature", lambda key: False)
    assert test_client.get(root + "/coverage", headers=headers).status_code == 402
