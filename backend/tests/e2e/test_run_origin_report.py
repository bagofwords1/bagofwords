"""POST /tests/runs/batch — the origin conversation a UI-started run reports to.

A run created from the eval strip used to be anonymous: no origin report, no
wake, so its result existed only in the browser tab that launched it and was
gone on reload. Naming the conversation makes the transcript the durable record,
the way an agent-initiated ``run_eval`` already does.

Finishing the run POSTS A COMPLETION into that conversation, so the field is a
write and is gated on OWNERSHIP — not merely on being able to see the report.
These tests pin that gate, which is the part that must not regress.

The happy path is deliberately not asserted here: the route executes the run,
so it needs a configured default model (see the same note in
``test_rbac_evals.py``), which this suite has no LLM for. That the columns are
actually populated is verified against a live sandbox instead.
"""
import uuid

import pytest


def _batch(test_client, token, org_id, **body):
    return test_client.post(
        "/api/tests/runs/batch",
        json=body,
        headers={"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)},
    )


@pytest.fixture
def batch_setup(create_user, login_user, whoami, create_test_suite, create_test_case, create_report):
    user = create_user()
    token = login_user(user["email"], user["password"])
    me = whoami(token)
    org_id = me["organizations"][0]["id"]
    auth = dict(user_token=token, org_id=org_id)

    suite = create_test_suite(name="Origin Suite", **auth)
    case = create_test_case(suite_id=suite["id"], name="origin case", **auth)
    report = create_report(title="Training chat", **auth)
    return {"token": token, "org_id": org_id, "case": case, "report": report, "user_id": me["id"]}


@pytest.mark.e2e
def test_cannot_wake_a_conversation_you_do_not_own(
    test_client, create_user, login_user, batch_setup,
):
    """Another admin — who passes the ``manage_evals`` gate and could happily
    start this run — still cannot aim its completion at someone else's thread."""
    s = batch_setup
    email = f"other_admin_{uuid.uuid4().hex[:6]}@test.com"
    invite = test_client.post(
        f"/api/organizations/{s['org_id']}/members",
        json={"organization_id": s["org_id"], "email": email, "role": "admin"},
        headers={"Authorization": f"Bearer {s['token']}", "X-Organization-Id": str(s["org_id"])},
    )
    assert invite.status_code == 200, invite.json()
    create_user(email=email, password="test123")
    other_token = login_user(email=email, password="test123")

    resp = _batch(
        test_client, other_token, s["org_id"],
        case_ids=[s["case"]["id"]],
        origin_report_id=s["report"]["id"], wake_on_finish=True,
    )
    assert resp.status_code == 404, resp.text

    # ...and the same caller CAN start the run when they don't claim an origin.
    # The 404 above is the origin check, not a permissions failure on the run
    # itself — otherwise this test would pass for the wrong reason. The run
    # still needs an LLM to execute, so anything but 403/404 proves the point.
    ok = _batch(test_client, other_token, s["org_id"], case_ids=[s["case"]["id"]])
    assert ok.status_code not in (403, 404), ok.text


@pytest.mark.e2e
def test_no_event_is_left_behind_when_the_origin_is_refused(test_client, batch_setup):
    """The "X started an eval run" strip is written only for an accepted
    origin. A rejected one must not leave a strip in anybody's conversation."""
    import os
    from sqlalchemy import create_engine, text

    s = batch_setup
    _batch(
        test_client, s["token"], s["org_id"],
        case_ids=[s["case"]["id"]],
        origin_report_id=str(uuid.uuid4()), wake_on_finish=True,
    )
    url = os.environ["TEST_DATABASE_URL"]
    sync_url = url.replace("sqlite+aiosqlite:", "sqlite:").replace("postgresql+asyncpg:", "postgresql:")
    engine = create_engine(sync_url)
    try:
        with engine.connect() as conn:
            n = conn.execute(
                text("SELECT count(*) FROM completions WHERE message_type = 'eval_run_started'")
            ).scalar()
    finally:
        engine.dispose()
    assert n == 0


@pytest.mark.e2e
def test_unknown_origin_report_is_rejected(test_client, batch_setup):
    s = batch_setup
    resp = _batch(
        test_client, s["token"], s["org_id"],
        case_ids=[s["case"]["id"]],
        origin_report_id=str(uuid.uuid4()), wake_on_finish=True,
    )
    assert resp.status_code == 404, resp.text
