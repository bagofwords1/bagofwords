"""End-to-end: the agent's reply leaves through the WhatsApp number messaged.

An organization can connect several WhatsApp numbers. Inbound routing already
resolves the exact number (and stamps it on ``Report.external_platform_id``),
but the reply path looked the platform up by organization + type only. With
two numbers that lookup raised ``MultipleResultsFound`` inside a swallowing
``except`` — the run finished, the question showed in monitoring, and the user
got no answer on WhatsApp.

Contract under test:
  * with two numbers, a reply to a conversation that came in on number B is
    delivered, and every outbound message uses number B's ``phone_number_id``;
  * a conversation with no recorded number (pre-routing report) still gets an
    answer through the org's oldest number instead of failing silently.

Mocked boundaries only: Meta's credential probe, Meta's send API
(``WhatsAppAdapter._post_message``) and the agent run itself. Routing, the
report, the persisted completion/blocks and the reply path are real.
"""
import asyncio
import uuid

import pytest

from tests.e2e.test_whatsapp_agent_scope import (  # noqa: F401  (fixtures)
    _headers,
    _inbound_event,
    _post_inbound,
    _seed_verified_mapping,
    inbound_boundaries,
    mock_whatsapp_probe,
)

ANSWER = "Total revenue last month was 42,000 across all regions."


@pytest.fixture
def sent_messages(monkeypatch):
    """Record (phone_number_id, payload) for every outbound WhatsApp message."""
    from app.services.platform_adapters.adapter_factory import PlatformAdapterFactory

    sent = []

    async def _record(self, payload):
        sent.append((self._phone_number_id(), payload))
        return True

    # Patch the class the factory actually instantiates: a unit test loads a
    # second copy of whatsapp_adapter into sys.modules, so importing the class
    # by module path can return a different object. Installed after
    # inbound_boundaries' stub so this one wins.
    monkeypatch.setattr(PlatformAdapterFactory._adapters["whatsapp"], "_post_message", _record)
    return sent


def _connect_two_numbers(test_client, token, org_id):
    numbers = {}
    for label in ("first", "second"):
        pnid = "PNID_" + uuid.uuid4().hex[:8]
        r = test_client.post(
            "/api/settings/integrations/whatsapp",
            json={"access_token": "tok-" + label, "phone_number_id": pnid,
                  "waba_id": "waba-1", "app_secret": "secret",
                  "verify_token": "verify-" + pnid, "name": label},
            headers=_headers(token, org_id),
        )
        assert r.status_code == 200, r.json()
        numbers[label] = {"pnid": pnid, "id": r.json()["id"]}
    return numbers


def _finish_run(report_id, wa_id, *, clear_report_platform=False):
    """Persist what a finished agent run leaves behind, then run the reply path.

    Writes the system completion + its final answer block (as agent_v2 does)
    and invokes the finish hook the completion's status flip triggers.
    """
    from app.dependencies import async_session_maker
    from app.models import completion_block as cb
    from app.models.completion import Completion
    from app.models.completion_block import CompletionBlock
    from app.models.report import Report

    async def _run():
        async with async_session_maker() as db:
            if clear_report_platform:
                report = await db.get(Report, report_id)
                report.external_platform_id = None
            comp = Completion(
                prompt=None, completion={"content": ANSWER}, status="success",
                role="system", report_id=report_id, turn_index=1,
                external_platform="whatsapp", external_user_id=wa_id,
                external_channel_id=wa_id, external_channel_type="im",
                external_thread_ts="wamid.root", external_message_ts="wamid.root",
            )
            db.add(comp)
            await db.flush()
            db.add(CompletionBlock(
                completion_id=comp.id, source_type="final", block_index=0,
                title="Answer", status="completed", content=ANSWER,
            ))
            await db.commit()
            comp_id = str(comp.id)

        await cb.send_completion_blocks_to_slack(comp_id)
        # Let the per-block listener the insert spawned finish too.
        await asyncio.sleep(0.5)

    asyncio.run(_run())


def _texts(sent):
    return [p["text"]["body"] for _, p in sent if p.get("type") == "text"]


@pytest.mark.e2e
def test_reply_goes_out_through_the_number_messaged(
    mock_whatsapp_probe, inbound_boundaries, sent_messages,
    create_user, login_user, whoami, test_client,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    me = whoami(token)
    org_id = me["organizations"][0]["id"]
    numbers = _connect_two_numbers(test_client, token, org_id)

    # Message the SECOND (newer) number — the one an oldest-row fallback
    # would get wrong.
    wa_id = "1555" + uuid.uuid4().hex[:7]
    _seed_verified_mapping(org_id, numbers["second"]["id"], wa_id, me["id"])
    r = _post_inbound(test_client, _inbound_event(numbers["second"]["pnid"], wa_id, "revenue?"))
    assert r.status_code == 200, r.json()
    assert r.json()["result"].get("success") is True, r.json()
    report_id = inbound_boundaries[-1]

    sent_messages.clear()  # drop the inbound ack, keep only the reply
    _finish_run(report_id, wa_id)

    assert ANSWER in _texts(sent_messages), sent_messages
    used = {pnid for pnid, _ in sent_messages}
    assert used == {numbers["second"]["pnid"]}, used


@pytest.mark.e2e
def test_reply_without_recorded_number_uses_oldest_number(
    mock_whatsapp_probe, inbound_boundaries, sent_messages,
    create_user, login_user, whoami, test_client,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    me = whoami(token)
    org_id = me["organizations"][0]["id"]
    numbers = _connect_two_numbers(test_client, token, org_id)

    wa_id = "1555" + uuid.uuid4().hex[:7]
    _seed_verified_mapping(org_id, numbers["first"]["id"], wa_id, me["id"])
    r = _post_inbound(test_client, _inbound_event(numbers["first"]["pnid"], wa_id, "revenue?"))
    assert r.status_code == 200, r.json()
    report_id = inbound_boundaries[-1]

    sent_messages.clear()
    _finish_run(report_id, wa_id, clear_report_platform=True)

    assert ANSWER in _texts(sent_messages), sent_messages
    assert {pnid for pnid, _ in sent_messages} == {numbers["first"]["pnid"]}
