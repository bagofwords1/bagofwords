"""GET /api/reports/{id}/turns — the turn index behind the transcript's
conversation map — and GET /completions?until=<id> (jump-to-message).

The map used to be built only from the paginated page in memory (10
completions), so on a long report it started mid-conversation. The index lists
every visible user prompt with short previews; `until` lets the page load back
to an older turn in one request.

Contract under test:
  * one entry per visible user prompt, oldest first, for the WHOLE report;
  * the same rows the feed hides stay hidden (webhook/machine trigger prompts,
    sub-agent children, steering, queued);
  * the answer preview is the turn's answer text, never an error block;
  * previews are short and PII-masked like the feed;
  * the report is not readable from another organization;
  * `until` serves a page reaching back to (and including) the target, and the
    cursor keeps paging correctly from there.

Seeding note: completions/blocks are written straight to the DB. Producing
40 answered turns, webhook triggers and sub-agent children through the API
would need real agent runs (an LLM); the report itself is created via the API.
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from app.dependencies import async_session_maker
from app.models.completion import Completion
from app.models.completion_block import CompletionBlock


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def owner(create_user, login_user, whoami, create_report):
    email = f"turns_{uuid.uuid4().hex[:8]}@example.com"
    create_user(email=email)
    token = login_user(email, "test123")
    org_id = whoami(token)["organizations"][0]["id"]
    report = create_report(title="Turns", user_token=token, org_id=org_id)
    return {"token": token, "org_id": org_id, "report_id": report["id"], "H": _headers(token, org_id)}


def _seed(report_id, n_turns, *, base=None, prompt_fmt="question {i}", answer_fmt="answer {i}",
          extras=True):
    """Seed `n_turns` answered prompts (plus, when `extras`, rows the timeline
    hides). Returns the ids of the visible prompts in order and the system ids."""
    base = base or datetime(2026, 3, 1, 9, 0, 0)

    async def run():
        visible, systems = [], []
        async with async_session_maker() as db:
            t = base
            for i in range(n_turns):
                u = Completion(prompt={"content": prompt_fmt.format(i=i)}, completion=None,
                               status="success", role="user", report_id=report_id, turn_index=i)
                u.created_at = u.updated_at = t
                s = Completion(prompt=None, completion={"content": ""}, status="success",
                               role="system", report_id=report_id, turn_index=i)
                s.created_at = s.updated_at = t + timedelta(seconds=1)
                db.add_all([u, s])
                await db.flush()
                # An error block first (must not become the preview), then the answer.
                db.add(CompletionBlock(completion_id=s.id, source_type="decision", block_index=0,
                                       title="t", status="error", content=f"boom {i}"))
                db.add(CompletionBlock(completion_id=s.id, source_type="decision", block_index=1,
                                       title="t", status="completed", content=answer_fmt.format(i=i)))
                visible.append(str(u.id))
                systems.append(str(s.id))
                t += timedelta(minutes=1)

                if extras and i == 2:
                    # Machine trigger prompt (webhook) — hidden by the feed.
                    w = Completion(prompt={"content": "HIDDEN webhook trigger"}, completion=None,
                                   status="success", role="user", report_id=report_id,
                                   trigger_source="webhook")
                    # Sub-agent child answer — hidden by the feed.
                    c = Completion(prompt=None, completion={"content": "HIDDEN child"}, status="success",
                                   role="system", report_id=report_id, trigger_source="sub_agent")
                    # Steering message and a queued prompt — not turns on the page either.
                    st = Completion(prompt={"content": "HIDDEN steering"}, completion=None,
                                    status="success", role="user", report_id=report_id,
                                    message_type="steering", parent_id=s.id)
                    q = Completion(prompt={"content": "HIDDEN queued"}, completion=None,
                                   status="queued", role="user", report_id=report_id)
                    for j, row in enumerate((w, c, st, q)):
                        row.created_at = row.updated_at = t + timedelta(seconds=j)
                    db.add_all([w, c, st, q])
                    t += timedelta(minutes=1)
            await db.commit()
        return visible, systems

    return asyncio.run(run())


@pytest.mark.e2e
def test_turns_lists_every_visible_prompt_of_a_long_report(test_client, owner):
    n = 43  # well past one feed page (10 completions)
    visible, _ = _seed(owner["report_id"], n)

    resp = test_client.get(f"/api/reports/{owner['report_id']}/turns", headers=owner["H"])
    assert resp.status_code == 200, resp.text
    turns = resp.json()["turns"]

    assert [t["completion_id"] for t in turns] == visible
    assert turns[0]["prompt_preview"] == "question 0"
    assert turns[0]["answer_preview"] == "answer 0"
    assert all("HIDDEN" not in t["prompt_preview"] + t["answer_preview"] for t in turns)
    assert all("boom" not in t["answer_preview"] for t in turns)

    # The feed's first page is only the tail; the index still starts at turn 1.
    page = test_client.get(f"/api/reports/{owner['report_id']}/completions?limit=10", headers=owner["H"]).json()
    assert page["has_more"] is True
    assert visible[0] not in {c["id"] for c in page["completions"]}


@pytest.mark.e2e
def test_turn_previews_are_bounded(test_client, owner):
    long = "word " * 400
    _seed(owner["report_id"], 2, prompt_fmt=long + "{i}", answer_fmt=long + "{i}", extras=False)

    turns = test_client.get(f"/api/reports/{owner['report_id']}/turns", headers=owner["H"]).json()["turns"]
    assert len(turns) == 2
    for t in turns:
        assert 0 < len(t["prompt_preview"]) <= 201
        assert 0 < len(t["answer_preview"]) <= 201


@pytest.fixture
def _pii_license():
    from app.ee import license as ee_license
    saved = (ee_license._cached_license, ee_license._cache_initialized)
    ee_license._cached_license = ee_license.LicenseInfo(
        licensed=True, tier="enterprise", org_name="turns-tests",
        features=["pii_protection"], license_id="turns-test",
    )
    ee_license._cache_initialized = True
    try:
        yield
    finally:
        ee_license._cached_license, ee_license._cache_initialized = saved


@pytest.mark.e2e
def test_turn_previews_apply_display_pii_masking(test_client, owner, _pii_license):
    import app.ai.llm.pii.loader as loader

    put = test_client.put("/api/organization/settings", headers=owner["H"], json={"config": {"pii_protection": {
        "enabled": True,
        "mode": "replace",
        "custom_rules": [{"id": "emp", "name": "Employee ID", "patterns": [r"EMP-\d{4}"],
                          "replacement": "[EMP]", "enabled": True}],
    }}})
    assert put.status_code == 200, put.text
    loader.invalidate()

    _seed(owner["report_id"], 3, prompt_fmt="who is EMP-100{i}?", answer_fmt="EMP-200{i} is on leave",
          extras=False)
    turns = test_client.get(f"/api/reports/{owner['report_id']}/turns", headers=owner["H"]).json()["turns"]
    assert len(turns) == 3
    for t in turns:
        assert "EMP-" not in t["prompt_preview"] and "[EMP]" in t["prompt_preview"]
        assert "EMP-" not in t["answer_preview"] and "[EMP]" in t["answer_preview"]


@pytest.mark.e2e
@pytest.mark.parametrize("lead", [596, 1200])
def test_answer_preview_masks_a_match_far_into_a_long_block(test_client, owner, _pii_license, lead):
    """A value deep inside a long answer block — after leading whitespace that
    the preview strips — must still be masked: redaction has to see the whole
    field before anything is cut, wherever a cut would fall."""
    import app.ai.llm.pii.loader as loader

    put = test_client.put("/api/organization/settings", headers=owner["H"], json={"config": {"pii_protection": {
        "enabled": True,
        "mode": "replace",
        "custom_rules": [{"id": "emp", "name": "Employee ID", "patterns": [r"EMP-\d{4}"],
                          "replacement": "[EMP]", "enabled": True}],
    }}})
    assert put.status_code == 200, put.text
    loader.invalidate()

    _seed(owner["report_id"], 1, answer_fmt="\n" * lead + "EMP-1234 is on leave", extras=False)
    turns = test_client.get(f"/api/reports/{owner['report_id']}/turns", headers=owner["H"]).json()["turns"]
    assert len(turns) == 1
    assert "EMP-" not in turns[0]["answer_preview"]
    assert "[EMP]" in turns[0]["answer_preview"]


@pytest.mark.e2e
def test_turns_not_readable_from_another_organization(test_client, owner, create_organization, monkeypatch):
    from app.settings.config import settings
    monkeypatch.setattr(settings.bow_config.features, "allow_multiple_organizations", True)
    _seed(owner["report_id"], 2, extras=False)
    other_org = create_organization(user_token=owner["token"])

    resp = test_client.get(f"/api/reports/{owner['report_id']}/turns",
                           headers=_headers(owner["token"], other_org))
    assert resp.status_code == 404, resp.text

    missing = test_client.get(f"/api/reports/{uuid.uuid4()}/turns", headers=owner["H"])
    assert missing.status_code == 404


@pytest.mark.e2e
def test_until_reaches_back_to_the_target_and_pagination_continues(test_client, owner):
    visible, _ = _seed(owner["report_id"], 30)
    url = f"/api/reports/{owner['report_id']}/completions"

    for target_idx in (0, 7, 25):
        target = visible[target_idx]
        page = test_client.get(f"{url}?limit=10&until={target}", headers=owner["H"]).json()
        ids = [c["id"] for c in page["completions"]]
        assert target in ids, f"turn {target_idx} not served by until="
        # The page is contiguous from the target to the newest row.
        assert ids[0] == target
        assert page["has_more"] is (target_idx > 0)

        # Paging on from the returned cursor serves the rest exactly once.
        seen = set(ids)
        before = page["next_before"]
        while page["has_more"]:
            page = test_client.get(f"{url}?limit=10&before={before}", headers=owner["H"]).json()
            new = {c["id"] for c in page["completions"]}
            assert not (new & seen)
            seen |= new
            before = page["next_before"]
        assert set(visible) <= seen

    # A target deeper than the default page but a short span still gets a full page.
    tail = test_client.get(f"{url}?limit=10&until={visible[-1]}", headers=owner["H"]).json()
    assert len(tail["completions"]) == 10

    # Unknown / foreign ids are ignored: the normal newest page is served.
    plain = test_client.get(f"{url}?limit=10", headers=owner["H"]).json()
    bogus = test_client.get(f"{url}?limit=10&until={uuid.uuid4()}", headers=owner["H"]).json()
    assert [c["id"] for c in bogus["completions"]] == [c["id"] for c in plain["completions"]]
