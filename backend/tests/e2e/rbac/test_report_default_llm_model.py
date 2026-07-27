"""
E2E tests for the report-level default LLM model (reports.model_id).

Behavior under test:
- a report can carry its own LLM override (`PUT /reports/{id}` with `model_id`),
  set from the models the caller can use, and cleared with an empty string;
- resolution precedence for a completion run is
  report.model_id > user default > org default: the report override wins over
  a user's personal default, which in turn wins over the org default;
- setting is strict about access (unknown → 404, disabled → 400, restricted
  without a grant → 403), while resolution is lenient: an override that later
  became stale (model disabled/restricted) silently falls back to the user
  default, then the org default — a report preference must never break chat.

Mirrors test_user_default_llm_model.py; providers are created with a dummy key.
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _run(coro):
    return asyncio.run(coro)


# ── helpers ──────────────────────────────────────────────────────────────


def _create_provider_with_models(test_client, token, org_id, n_models=3):
    suffix = uuid.uuid4().hex[:6]
    models = [
        {"model_id": f"claude-test-{suffix}-{i}", "name": f"Test Model {suffix} {i}", "is_custom": True}
        for i in range(n_models)
    ]
    resp = test_client.post(
        "/api/llm/providers",
        json={
            "name": f"prov-{suffix}",
            "provider_type": "anthropic",
            "credentials": {"api_key": "dummy-key"},
            "models": models,
        },
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _list_models(test_client, token, org_id):
    resp = test_client.get("/api/llm/models", headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _create_report(test_client, token, org_id, title="Report LLM Test"):
    resp = test_client.post(
        "/api/reports",
        json={"title": title, "files": [], "data_sources": []},
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _put_report_model(test_client, token, org_id, report_id, model_id):
    return test_client.put(
        f"/api/reports/{report_id}",
        json={"model_id": model_id},
        headers=_headers(token, org_id),
    )


def _get_report(test_client, token, org_id, report_id):
    resp = test_client.get(f"/api/reports/{report_id}", headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _put_user_default(test_client, token, org_id, model_id):
    return test_client.put(
        "/api/users/me/default_model",
        json={"model_id": model_id},
        headers=_headers(token, org_id),
    )


def _restrict(test_client, token, org_id, model_id, value=True):
    return test_client.put(
        f"/api/llm/models/{model_id}/restricted",
        json={"is_restricted": value},
        headers=_headers(token, org_id),
    )


def _add_access(test_client, token, org_id, model_id, principal_type, principal_id):
    return test_client.post(
        f"/api/llm/models/{model_id}/access",
        json={"principal_type": principal_type, "principal_id": principal_id},
        headers=_headers(token, org_id),
    )


def _toggle(test_client, token, org_id, model_id, enabled):
    return test_client.post(
        f"/api/llm/models/{model_id}/toggle?enabled={'true' if enabled else 'false'}",
        headers=_headers(token, org_id),
    )


async def _resolve_for_report(org_id, user_id, report_id):
    """Resolve the effective model via the same service call the chat paths use."""
    from app.services.llm_service import LLMService
    from app.models.organization import Organization
    from app.models.user import User
    from app.models.report import Report

    async with async_session_maker() as db:
        organization = await db.get(Organization, org_id)
        user = await db.get(User, user_id)
        report = await db.get(Report, report_id)
        return await LLMService().get_default_model_for_report(db, organization, user, report)


@pytest.fixture
def cast(test_client, bootstrap_admin, invite_user_to_org):
    """Admin + regular member in one org, plus a provider with 3 models."""
    admin = bootstrap_admin("reportdef")
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    _create_provider_with_models(test_client, admin["token"], admin["org_id"])
    models = _list_models(test_client, admin["token"], admin["org_id"])
    non_default = next(
        m for m in models if not m["is_default"] and not m["is_small_default"]
    )
    others = [
        m for m in models
        if not m["is_default"] and not m["is_small_default"] and m["id"] != non_default["id"]
    ]
    return {
        "admin": admin,
        "member": member,
        "org_id": admin["org_id"],
        "models": models,
        "org_default_id": next(m["id"] for m in models if m["is_default"]),
        "non_default_id": non_default["id"],
        "second_non_default_id": others[0]["id"] if others else non_default["id"],
    }


# ── set / clear / read-back ──────────────────────────────────────────────


@pytest.mark.e2e
def test_report_model_set_clear_and_readback(test_client, cast):
    admin_t, org = cast["admin"]["token"], cast["org_id"]
    report = _create_report(test_client, admin_t, org)
    target = cast["non_default_id"]

    # No override initially → resolves to the org default.
    assert _get_report(test_client, admin_t, org, report["id"]).get("model_id") is None
    resolved = _run(_resolve_for_report(org, cast["admin"]["user_id"], report["id"]))
    assert str(resolved.id) == cast["org_default_id"]

    # Set → persisted, readable back, and resolution honors it.
    resp = _put_report_model(test_client, admin_t, org, report["id"], target)
    assert resp.status_code == 200, resp.json()
    assert _get_report(test_client, admin_t, org, report["id"])["model_id"] == target
    resolved = _run(_resolve_for_report(org, cast["admin"]["user_id"], report["id"]))
    assert str(resolved.id) == target

    # Clear with "" → back to org default.
    resp = _put_report_model(test_client, admin_t, org, report["id"], "")
    assert resp.status_code == 200, resp.json()
    assert _get_report(test_client, admin_t, org, report["id"]).get("model_id") is None
    resolved = _run(_resolve_for_report(org, cast["admin"]["user_id"], report["id"]))
    assert str(resolved.id) == cast["org_default_id"]


@pytest.mark.e2e
def test_omitting_model_id_leaves_override_untouched(test_client, cast):
    """A PUT without model_id (e.g. a title edit) must not wipe the override."""
    admin_t, org = cast["admin"]["token"], cast["org_id"]
    report = _create_report(test_client, admin_t, org)
    target = cast["non_default_id"]

    assert _put_report_model(test_client, admin_t, org, report["id"], target).status_code == 200

    # Title-only update — model_id omitted entirely.
    resp = test_client.put(
        f"/api/reports/{report['id']}",
        json={"title": "renamed"},
        headers=_headers(admin_t, org),
    )
    assert resp.status_code == 200, resp.json()
    assert _get_report(test_client, admin_t, org, report["id"])["model_id"] == target


# ── precedence: report > user > org ──────────────────────────────────────


@pytest.mark.e2e
def test_report_model_overrides_user_default(test_client, cast):
    """report.model_id must win over the user's personal default, which wins
    over the org default."""
    member, org = cast["member"], cast["org_id"]
    member_t = member["token"]
    user_pref = cast["non_default_id"]
    report_pref = cast["second_non_default_id"]

    report = _create_report(test_client, member_t, org)

    # Only a user default set → resolves to the user default (user > org).
    assert _put_user_default(test_client, member_t, org, user_pref).status_code == 200
    resolved = _run(_resolve_for_report(org, member["user_id"], report["id"]))
    assert str(resolved.id) == user_pref

    # Add a report override → it wins over the user default (report > user).
    assert _put_report_model(test_client, member_t, org, report["id"], report_pref).status_code == 200
    resolved = _run(_resolve_for_report(org, member["user_id"], report["id"]))
    assert str(resolved.id) == report_pref

    # Clear the report override → falls back to the user default, not org.
    assert _put_report_model(test_client, member_t, org, report["id"], "").status_code == 200
    resolved = _run(_resolve_for_report(org, member["user_id"], report["id"]))
    assert str(resolved.id) == user_pref


# ── strict write / lenient read ──────────────────────────────────────────


@pytest.mark.e2e
def test_set_rejects_unknown_and_disabled_models(test_client, cast):
    admin_t, org = cast["admin"]["token"], cast["org_id"]
    report = _create_report(test_client, admin_t, org)

    # Unknown model → 404.
    assert _put_report_model(test_client, admin_t, org, report["id"], str(uuid.uuid4())).status_code == 404

    # Disabled model → 400.
    target = cast["non_default_id"]
    assert _toggle(test_client, admin_t, org, target, enabled=False).status_code == 200
    assert _put_report_model(test_client, admin_t, org, report["id"], target).status_code == 400


@pytest.mark.e2e
def test_stale_report_model_falls_back(test_client, cast):
    """An override that becomes unusable (model disabled after being set)
    resolves to the org default instead of breaking chat."""
    admin_t, org = cast["admin"]["token"], cast["org_id"]
    report = _create_report(test_client, admin_t, org)
    target = cast["non_default_id"]

    assert _put_report_model(test_client, admin_t, org, report["id"], target).status_code == 200
    resolved = _run(_resolve_for_report(org, cast["admin"]["user_id"], report["id"]))
    assert str(resolved.id) == target

    # Disable the chosen model out from under the report.
    assert _toggle(test_client, admin_t, org, target, enabled=False).status_code == 200
    resolved = _run(_resolve_for_report(org, cast["admin"]["user_id"], report["id"]))
    assert str(resolved.id) == cast["org_default_id"]  # degraded, did not raise


# ── access control (restricted models, group grants) ─────────────────────


@pytest.mark.e2e
def test_restricted_model_requires_grant_then_degrades_on_revoke(
    enterprise_license, test_client, cast, create_group, add_user_to_group
):
    """A member cannot set a restricted model as the report override without a
    grant; a group grant allows it; revoking the grant degrades resolution back
    to the org default without leaking the restricted model."""
    member, admin_t, org = cast["member"], cast["admin"]["token"], cast["org_id"]
    member_t = member["token"]
    target = cast["non_default_id"]
    report = _create_report(test_client, member_t, org)

    assert _restrict(test_client, admin_t, org, target, True).status_code == 200

    # No grant → cannot set as report override.
    assert _put_report_model(test_client, member_t, org, report["id"], target).status_code == 403

    # Group grant → now settable, and resolution honors it.
    grp = create_group(name=f"grp-{uuid.uuid4().hex[:6]}", user_token=admin_t, org_id=org)
    group_id = grp.json()["id"] if hasattr(grp, "json") else grp["id"]
    add_user_to_group(group_id=group_id, user_id=member["user_id"], user_token=admin_t, org_id=org)
    assert _add_access(test_client, admin_t, org, target, "group", group_id).status_code == 200
    assert _put_report_model(test_client, member_t, org, report["id"], target).status_code == 200
    resolved = _run(_resolve_for_report(org, member["user_id"], report["id"]))
    assert str(resolved.id) == target
