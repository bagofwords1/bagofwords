"""Searching inside one report (⌘K "In this report") and jumping to a hit.

GET /api/reports/{id}/completions/search must find every message the
transcript shows — including pages the client hasn't loaded — and nothing the
transcript hides. GET /api/reports/{id}/completions?until=<id> must return one
page that reaches down to that message.

No public API creates completions without an LLM run, so transcript rows are
written directly through the ORM (so the encrypted column types apply), the
same way tests/e2e/test_encrypted_columns_search.py seeds them.
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from app.dependencies import async_session_maker
from app.settings.config import settings as bow_settings
from app.ee.encryption import types as enc
from app.models.completion import Completion
from app.models.completion_block import CompletionBlock

BASE_TIME = datetime(2026, 1, 1, 9, 0, 0)


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _owner(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    return token, whoami(token)["organizations"][0]["id"]


def _seed(report_id, rows):
    """rows: dicts with role, text, minute, and optional webhook/trigger_source/
    blocks (system answer block texts). Returns the created ids in order."""
    async def _run():
        ids = []
        async with async_session_maker() as db:
            for row in rows:
                cid = str(uuid.uuid4())
                role = row["role"]
                text = row.get("text", "")
                db.add(Completion(
                    id=cid, report_id=report_id, role=role,
                    prompt={"content": text if role != "system" else "", "mentions": []},
                    completion={"content": text if role != "user" else ""},
                    status="success", model="m", turn_index=0, feedback_score=0,
                    message_type="user_message" if role == "user" else "ai_completion",
                    trigger_source=row.get("trigger_source"),
                    created_at=BASE_TIME + timedelta(minutes=row["minute"]),
                ))
                await db.flush()
                for i, block_text in enumerate(row.get("blocks", [])):
                    db.add(CompletionBlock(
                        id=str(uuid.uuid4()), completion_id=cid, source_type="decision",
                        block_index=i, title="Answer", status="completed", content=block_text,
                    ))
                ids.append(cid)
            await db.commit()
        return ids
    return asyncio.run(_run())


def _search(client, token, org_id, report_id, q, **params):
    resp = client.get(
        f"/api/reports/{report_id}/completions/search",
        params={"q": q, **params}, headers=_h(token, org_id),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _long_conversation(report_id, needle, turns=36):
    """`turns` alternating user/system rows; the needle is only in the first."""
    rows = []
    for i in range(turns):
        role = "user" if i % 2 == 0 else "system"
        text = f"first question about {needle} retention" if i == 0 else f"ordinary turn {i}"
        rows.append({"role": role, "minute": i, "text": text,
                     "blocks": [text] if role == "system" else []})
    return _seed(report_id, rows)


@pytest.mark.e2e
def test_search_finds_messages_on_pages_the_client_never_loaded(
    test_client, create_user, login_user, whoami, create_report,
):
    token, org_id = _owner(create_user, login_user, whoami)
    report_id = create_report(title="long", user_token=token, org_id=org_id)["id"]
    needle = f"churn{uuid.uuid4().hex[:6]}"
    ids = _long_conversation(report_id, needle)

    # The first page the transcript loads does not contain the first prompt.
    first_page = test_client.get(
        f"/api/reports/{report_id}/completions", params={"limit": 10}, headers=_h(token, org_id),
    ).json()
    assert ids[0] not in {c["id"] for c in first_page["completions"]}

    hits = _search(test_client, token, org_id, report_id, needle.upper())  # case-insensitive
    assert [h["completion_id"] for h in hits] == [ids[0]]
    hit = hits[0]
    assert hit["role"] == "user"
    assert needle in hit["snippet"]
    ts, _, cid = hit["cursor"].partition("~")
    assert cid == ids[0] and datetime.fromisoformat(ts)


@pytest.mark.e2e
def test_until_returns_one_page_down_to_the_target_message(
    test_client, create_user, login_user, whoami, create_report,
):
    token, org_id = _owner(create_user, login_user, whoami)
    report_id = create_report(title="long", user_token=token, org_id=org_id)["id"]
    ids = _long_conversation(report_id, "x", turns=36)

    for target_index in (0, 7, 30):
        page = test_client.get(
            f"/api/reports/{report_id}/completions",
            params={"limit": 10, "until": ids[target_index]}, headers=_h(token, org_id),
        ).json()
        got = [c["id"] for c in page["completions"]]
        # Contiguous from the newest message down to (at least) the target.
        assert ids[target_index] in got
        assert got == ids[len(ids) - len(got):]
        assert len(got) >= len(ids) - target_index
        assert page["has_more"] == (got[0] != ids[0])

    # Unknown ids fall back to a normal page.
    page = test_client.get(
        f"/api/reports/{report_id}/completions",
        params={"limit": 10, "until": str(uuid.uuid4())}, headers=_h(token, org_id),
    ).json()
    assert [c["id"] for c in page["completions"]] == ids[-10:]

    # `until` composes with `before`: the page starts below the cursor.
    newest_page = test_client.get(
        f"/api/reports/{report_id}/completions", params={"limit": 10}, headers=_h(token, org_id),
    ).json()
    older = test_client.get(
        f"/api/reports/{report_id}/completions",
        params={"limit": 10, "before": newest_page["next_before"], "until": ids[3]},
        headers=_h(token, org_id),
    ).json()
    assert [c["id"] for c in older["completions"]] == ids[3:-10]


@pytest.mark.e2e
def test_search_matches_system_answer_blocks_and_orders_newest_first(
    test_client, create_user, login_user, whoami, create_report,
):
    token, org_id = _owner(create_user, login_user, whoami)
    report_id = create_report(title="answers", user_token=token, org_id=org_id)["id"]
    needle = f"margin{uuid.uuid4().hex[:6]}"
    asked, answered, later = _seed(report_id, [
        {"role": "user", "minute": 0, "text": f"what drives {needle}?"},
        # The term appears only in an answer block, not in the completion JSON.
        {"role": "system", "minute": 1, "text": "", "blocks": ["Intro.", f"The {needle} fell 4%."]},
        {"role": "user", "minute": 2, "text": f"and {needle} by region"},
    ])

    hits = _search(test_client, token, org_id, report_id, needle)
    assert [h["completion_id"] for h in hits] == [later, answered, asked]
    assert hits[1]["role"] == "system" and needle in hits[1]["snippet"]

    assert len(_search(test_client, token, org_id, report_id, needle, limit=2)) == 2
    # Keys of the stored JSON are not message text.
    assert _search(test_client, token, org_id, report_id, "mentions") == []
    assert _search(test_client, token, org_id, report_id, "   ") == []


@pytest.mark.e2e
def test_search_skips_rows_the_transcript_hides(
    test_client, create_user, login_user, whoami, create_report,
):
    token, org_id = _owner(create_user, login_user, whoami)
    report_id = create_report(title="hidden", user_token=token, org_id=org_id)["id"]
    needle = f"invoice{uuid.uuid4().hex[:6]}"
    _, _, visible_reply, visible_user = _seed(report_id, [
        # Synthetic prompt a trigger sends the agent: hidden.
        {"role": "user", "minute": 0, "text": f"[trigger] {needle}", "trigger_source": "scheduled"},
        # A sub-agent child's answer: hidden (its delegate card is the surface).
        {"role": "system", "minute": 1, "text": needle, "blocks": [needle], "trigger_source": "sub_agent"},
        # The agent's reply to the trigger: shown.
        {"role": "system", "minute": 2, "text": needle, "blocks": [needle], "trigger_source": "scheduled"},
        {"role": "user", "minute": 3, "text": f"about {needle}"},
    ])

    found = {h["completion_id"] for h in _search(test_client, token, org_id, report_id, needle)}
    shown = {c["id"] for c in test_client.get(
        f"/api/reports/{report_id}/completions", params={"limit": 50}, headers=_h(token, org_id),
    ).json()["completions"]}
    assert found == {visible_reply, visible_user}
    assert found <= shown


@pytest.mark.e2e
def test_search_finds_encrypted_and_plaintext_messages(
    test_client, create_user, login_user, whoami, create_report, monkeypatch,
):
    token, org_id = _owner(create_user, login_user, whoami)
    report_id = create_report(title="cipher", user_token=token, org_id=org_id)["id"]
    needle = f"zebra{uuid.uuid4().hex[:6]}"

    monkeypatch.setattr(enc, "encryption_active", lambda: False)
    (plain,) = _seed(report_id, [{"role": "user", "minute": 0, "text": f"plain {needle}"}])
    monkeypatch.setattr(enc, "encryption_active", lambda: True)
    (cipher,) = _seed(report_id, [{"role": "user", "minute": 1, "text": f"cipher {needle}"}])

    for active in (True, False):
        monkeypatch.setattr(enc, "encryption_active", lambda active=active: active)
        found = {h["completion_id"] for h in _search(test_client, token, org_id, report_id, needle)}
        assert found == {plain, cipher}


@pytest.fixture
def _allow_outsiders():
    """Let a user outside the org sign up and own a second org, to build the
    cross-org scenario."""
    flags = bow_settings.bow_config.features
    saved = (flags.allow_multiple_organizations, flags.allow_uninvited_signups)
    flags.allow_multiple_organizations = True
    flags.allow_uninvited_signups = True
    try:
        yield
    finally:
        flags.allow_multiple_organizations, flags.allow_uninvited_signups = saved


@pytest.mark.e2e
def test_search_access_matches_transcript_access(
    _allow_outsiders, test_client, create_user, login_user, whoami, create_report, create_organization,
):
    """Whoever can read the transcript can search it, with the same results;
    nobody else can."""
    admin_token, org_id = _owner(create_user, login_user, whoami)
    member_email = f"member_{uuid.uuid4().hex[:6]}@test.com"
    test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers=_h(admin_token, org_id),
    )
    create_user(email=member_email, password="test123")
    member_token = login_user(email=member_email, password="test123")
    outsider_email = f"outsider_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=outsider_email, password="test123")
    outsider_token = login_user(email=outsider_email, password="test123")
    outsider_org = create_organization(name=f"other-{uuid.uuid4().hex[:6]}", user_token=outsider_token)

    needle = f"quota{uuid.uuid4().hex[:6]}"
    admins_report = create_report(title="admin's", user_token=admin_token, org_id=org_id)["id"]
    members_report = create_report(title="member's", user_token=member_token, org_id=org_id)["id"]
    _seed(admins_report, [{"role": "user", "minute": 0, "text": needle}])
    _seed(members_report, [{"role": "user", "minute": 0, "text": needle}])
    url = lambda rid: f"/api/reports/{rid}/completions/search"

    # Each check pairs the search with the transcript read it must agree with.
    cases = [
        (member_token, org_id, members_report, 200),   # owner
        (admin_token, org_id, members_report, 200),    # org admin may view
        (member_token, org_id, admins_report, 403),    # member, not owner
        (outsider_token, outsider_org, admins_report, 404),  # other org
        (outsider_token, org_id, admins_report, 403),  # not a member
    ]
    for token, org, rid, expected in cases:
        transcript = test_client.get(f"/api/reports/{rid}/completions", headers=_h(token, org))
        search = test_client.get(url(rid), params={"q": needle}, headers=_h(token, org))
        assert search.status_code == transcript.status_code == expected, (rid, search.text)
        if expected == 200:
            assert [h["completion_id"] for h in search.json()] == [
                c["id"] for c in transcript.json()["completions"]
            ]


@pytest.fixture
def _pii_licensed():
    """Same license pin as tests/e2e/test_pii_protection.py."""
    from app.ee import license as ee_license
    saved = (ee_license._cached_license, ee_license._cache_initialized)
    ee_license._cached_license = ee_license.LicenseInfo(
        licensed=True, tier="enterprise", org_name="pii-tests",
        features=["pii_protection"], license_id="pii-test",
    )
    ee_license._cache_initialized = True
    try:
        yield
    finally:
        ee_license._cached_license, ee_license._cache_initialized = saved


@pytest.mark.e2e
def test_search_sees_only_the_redacted_text_the_transcript_shows(
    _pii_licensed, test_client, create_user, login_user, whoami, create_report,
):
    import app.ai.llm.pii.loader as loader

    token, org_id = _owner(create_user, login_user, whoami)
    report_id = create_report(title="pii", user_token=token, org_id=org_id)["id"]
    needle = f"renewal{uuid.uuid4().hex[:6]}"
    email = f"jane.{uuid.uuid4().hex[:6]}@example.com"
    (cid,) = _seed(report_id, [{"role": "user", "minute": 0, "text": f"email {email} about the {needle}"}])

    resp = test_client.put(
        "/api/organization/settings", json={"config": {"pii_protection": {"enabled": True}}},
        headers=_h(token, org_id),
    )
    assert resp.status_code == 200, resp.text
    loader.invalidate()
    try:
        shown = test_client.get(
            f"/api/reports/{report_id}/completions", headers=_h(token, org_id),
        ).json()["completions"][0]["prompt"]["content"]
        assert email not in shown  # precondition: the transcript masks it

        hits = _search(test_client, token, org_id, report_id, needle)
        assert [h["completion_id"] for h in hits] == [cid]
        assert email not in hits[0]["snippet"]
        # The hidden value can't be confirmed by searching for it.
        assert _search(test_client, token, org_id, report_id, email) == []
    finally:
        loader.invalidate()
