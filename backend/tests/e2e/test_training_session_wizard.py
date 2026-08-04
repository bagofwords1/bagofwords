"""Embedded training session (Create Data Agent wizard, step 3).

Contract under test — the two endpoints backing the wizard's "Set Context"
step:

- ``GET  /data_sources/{id}/training_preview`` reports whether a guided
  session can start (org LLM present) plus the active-table count that drives
  the CTA copy. It must degrade gracefully without an LLM (no error, just
  ``llm_available: false`` and no domain guess).
- ``POST /data_sources/{id}/training_session`` creates a training-mode report
  titled ``Training "<agent>"`` and kicks it off with a *hidden* brief: the
  ``role='user'`` kickoff completion is stamped with ``trigger_source`` so the
  completions API filters it from the timeline, while a ``role='external'``
  event strip and the agent's ``role='system'`` turn remain visible. Without
  an org LLM the endpoint refuses with 400.

The LLM provider is a boundary: it is registered with a dummy key, so the
background agent turn and the domain guess fail gracefully — everything
asserted here is persisted before any provider call succeeds.
"""
import asyncio

import pytest
from sqlalchemy import select


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _seed_org_with_demo_agent(create_user, login_user, whoami, test_client,
                              list_demo_data_sources, install_demo_data_source,
                              bulk_update_tables):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    demos = list_demo_data_sources(user_token=token, org_id=org_id)
    assert demos, "expected at least one demo data source"
    installed = install_demo_data_source(demo_id=demos[0]["id"], user_token=token, org_id=org_id)
    ds_id = installed.get("data_source_id") or installed.get("id")
    assert ds_id

    # The wizard's step 2 activates tables; mirror that so the preview has a
    # real count to report.
    bulk_update_tables(data_source_id=ds_id, action="activate", user_token=token, org_id=org_id)
    return token, org_id, ds_id


def _register_dummy_llm(test_client, token, org_id):
    resp = test_client.post(
        "/api/llm/providers",
        json={
            "name": "dummy provider",
            "provider_type": "openai",
            "credentials": {"api_key": "sk-dummy-training-test"},
            "models": [{"model_id": "gpt-5.6-sol", "name": "GPT test", "is_custom": False, "is_default": True}],
        },
        headers=_hdr(token, org_id),
    )
    assert resp.status_code == 200, resp.json()


@pytest.mark.e2e
def test_training_preview_degrades_without_llm_and_reports_tables(
    create_user, login_user, whoami, test_client,
    list_demo_data_sources, install_demo_data_source, bulk_update_tables,
):
    token, org_id, ds_id = _seed_org_with_demo_agent(
        create_user, login_user, whoami, test_client,
        list_demo_data_sources, install_demo_data_source, bulk_update_tables,
    )

    resp = test_client.get(f"/api/data_sources/{ds_id}/training_preview", headers=_hdr(token, org_id))
    assert resp.status_code == 200, resp.json()
    preview = resp.json()
    assert preview["llm_available"] is False
    assert preview["domain_hint"] is None
    assert preview["table_count"] > 0
    assert preview["agent_name"]
    # Shape-aware counts: the demo is a database, so its catalog reads as
    # tables; counts must line up with the flat total.
    assert isinstance(preview["items"], list) and preview["items"]
    assert all(i["shape"] in ("tables", "objects", "files", "tools") for i in preview["items"])
    assert sum(i["count"] for i in preview["items"] if i["shape"] != "tools") == preview["table_count"]
    assert any(i["shape"] == "tables" and i["count"] > 0 for i in preview["items"])

    # Starting a session without an LLM must refuse, not crash.
    resp = test_client.post(f"/api/data_sources/{ds_id}/training_session", headers=_hdr(token, org_id))
    assert resp.status_code == 400

    # With an LLM registered the preview flips available (the domain guess may
    # still be None — the dummy key can't produce one, and that is fine).
    _register_dummy_llm(test_client, token, org_id)
    resp = test_client.get(f"/api/data_sources/{ds_id}/training_preview", headers=_hdr(token, org_id))
    assert resp.status_code == 200
    assert resp.json()["llm_available"] is True


@pytest.mark.e2e
def test_training_session_creates_training_report_with_hidden_kickoff(
    create_user, login_user, whoami, test_client,
    list_demo_data_sources, install_demo_data_source, bulk_update_tables,
):
    token, org_id, ds_id = _seed_org_with_demo_agent(
        create_user, login_user, whoami, test_client,
        list_demo_data_sources, install_demo_data_source, bulk_update_tables,
    )
    _register_dummy_llm(test_client, token, org_id)

    ds = test_client.get(f"/api/data_sources/{ds_id}", headers=_hdr(token, org_id)).json()

    resp = test_client.post(f"/api/data_sources/{ds_id}/training_session", headers=_hdr(token, org_id))
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    report_id = body["report_id"]
    assert body["title"] == f'Training "{ds["name"]}"'

    report = test_client.get(f"/api/reports/{report_id}", headers=_hdr(token, org_id)).json()
    assert report["mode"] == "training"
    assert report["title"] == body["title"]
    # Session runs pinned to a specific model (small default when present).
    assert report.get("model_id")
    assert any(d["id"] == ds_id for d in report.get("data_sources", []))

    # Timeline contract: the kickoff brief is HIDDEN — no user bubble; the
    # visible rows are the event strip and the agent turn.
    completions = test_client.get(
        f"/api/reports/{report_id}/completions", headers=_hdr(token, org_id)
    ).json()["completions"]
    roles = [c["role"] for c in completions]
    assert "user" not in roles
    assert "external" in roles
    assert "system" in roles
    event = next(c for c in completions if c["role"] == "external")
    assert event.get("trigger_source") == "training_session"

    # The hidden kickoff row exists underneath, stamped for filtering, and it
    # carries the training-mode brief.
    from app.dependencies import async_session_maker
    from app.models.completion import Completion

    async def _load_hidden():
        async with async_session_maker() as db:
            res = await db.execute(
                select(Completion).where(
                    Completion.report_id == report_id,
                    Completion.role == "user",
                )
            )
            return res.scalars().all()

    hidden = asyncio.run(_load_hidden())
    assert len(hidden) == 1
    assert hidden[0].trigger_source == "training_session"
    prompt = hidden[0].prompt or {}
    assert prompt.get("mode") == "training"
    assert "TRAINING SESSION" in (prompt.get("content") or "")
