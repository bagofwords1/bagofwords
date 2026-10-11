"""Public outputs apply the SAME viewer rules as the public report view.

A shared report reaches anonymous readers through three surfaces:

  * the public JSON view   GET /api/r/{id}/queries/{qid}/step   (get_public_step)
  * the public HTML export GET /api/r/{id}/export_html
  * the shared transcript  GET /api/c/{token}                    (get_public_conversation)

The JSON view resolves rows through viewer_data_policy.resolve_step_data, and
every owner-facing transcript serializer masks PII through the org's display
redactor (app.ai.llm.pii.display). These tests pin that the other two public
surfaces go through those same code paths, so the three cannot drift:

  * the export embeds exactly the rows the JSON view would serve the same
    reader — nothing for an identity-scoped query, everything for a plain one;
  * the shared transcript is masked by the same redactor the owner's timeline
    uses, and unmasked content is served unchanged.
"""
import json
import re
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.completion import Completion
from app.models.completion_block import CompletionBlock
from app.models.organization import Organization
from app.models.organization_settings import OrganizationSettings
from app.models.plan_decision import PlanDecision
from app.models.agent_execution import AgentExecution
from app.models.query import Query
from app.models.report import Report
from app.models.user import User
from app.services.report_service import ReportService
from tests.e2e.test_html_export_offline import (
    _export_public,
    _seed,
    requires_export_libs,
)

pytestmark = pytest.mark.e2e

IDENTITY_PARAMS = [{"name": "email", "type": "string", "source": "identity"}]
INPUT_DEFAULT_PARAMS = [{"name": "owner", "type": "string", "source": "input_identity_default"}]


def _embedded_payload(html: str) -> dict:
    m = re.search(r"window\.ARTIFACT_DATA = (.*?);\nwindow\.__BOW_INFO", html, re.S)
    assert m, "export carries no ARTIFACT_DATA"
    return json.loads(m.group(1))


async def _public_json_rows(report_id: str, user=None) -> dict:
    """What the public JSON view serves this reader, per query id."""
    from sqlalchemy import select

    svc = ReportService()
    async with async_session_maker() as db:
        qids = [r[0] for r in (await db.execute(
            select(Query.id).where(Query.report_id == report_id)
        )).all()]
        out = {}
        for qid in qids:
            step = await svc.get_public_step(db, report_id, str(qid), user=user)
            out[str(qid)] = (step.data or {}).get("rows", [])
        return out


async def _owner(report_id: str):
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        return await db.get(User, report.user_id)


@requires_export_libs
@pytest.mark.asyncio
@pytest.mark.parametrize("params", [IDENTITY_PARAMS, INPUT_DEFAULT_PARAMS])
async def test_public_export_withholds_identity_scoped_rows_like_the_json_view(params):
    report_id, _ = await _seed(parameters=params, artifact_visibility="public")

    json_rows = await _public_json_rows(report_id)
    assert all(rows == [] for rows in json_rows.values()), "precondition: JSON view withholds"

    resp = await _export_public(report_id)
    assert resp.status_code == 200
    payload = _embedded_payload(resp.body.decode("utf-8"))
    vizs = payload["visualizations"]
    assert vizs, "the dashboard still exports its visualizations"
    for viz in vizs:
        assert viz["rows"] == json_rows[viz["query_id"]] == []
    # The identity value the snapshot was built with never crosses either.
    assert "email" not in (payload["params"].get("values") or {}) or not payload["params"]["values"].get("email")


@requires_export_libs
@pytest.mark.asyncio
async def test_public_export_matches_the_json_view_for_plain_shared_rows():
    """Non-withheld content keeps exporting in full — charts still populate."""
    report_id, _ = await _seed(artifact_visibility="public")

    json_rows = await _public_json_rows(report_id)
    resp = await _export_public(report_id)
    assert resp.status_code == 200
    vizs = _embedded_payload(resp.body.decode("utf-8"))["visualizations"]
    assert vizs
    for viz in vizs:
        assert viz["rows"], "plain shared rows must still be exported"
        assert viz["rows"] == json_rows[viz["query_id"]]


@requires_export_libs
@pytest.mark.asyncio
async def test_owner_public_export_keeps_their_own_identity_scoped_rows():
    """The owner is entitled to their snapshot on every surface."""
    report_id, _ = await _seed(parameters=IDENTITY_PARAMS, artifact_visibility="public")
    owner = await _owner(report_id)

    json_rows = await _public_json_rows(report_id, user=owner)
    resp = await _export_public(report_id, user=owner)
    vizs = _embedded_payload(resp.body.decode("utf-8"))["visualizations"]
    for viz in vizs:
        assert viz["rows"], "owner keeps full data"
        assert viz["rows"] == json_rows[viz["query_id"]]


# ── shared conversation ───────────────────────────────────────────────────────

SECRET_EMAIL = "jane.doe.{}@acme-corp.com"
EMP_ID = "EMP-4821"


async def _seed_conversation(pii_enabled: bool = True):
    suffix = uuid.uuid4().hex[:8]
    token = f"tok-{uuid.uuid4().hex}"
    email = SECRET_EMAIL.format(suffix)
    async with async_session_maker() as db:
        org = Organization(name=f"PII Conv Org {suffix}")
        db.add(org)
        await db.flush()
        if pii_enabled:
            db.add(OrganizationSettings(organization_id=org.id, config={"pii_protection": {
                "enabled": True, "mode": "replace",
                "custom_rules": [{"id": "emp", "name": "Employee ID", "patterns": [r"EMP-\d{4}"],
                                  "replacement": "[EMP]", "enabled": True}],
            }}))
        user = User(name="Conv Owner", email=f"pii-{suffix}@example.com", hashed_password="x",
                    is_active=True, is_superuser=False, is_verified=True)
        db.add(user)
        await db.flush()
        report = Report(title="PII conv", slug=f"pii-conv-{suffix}", status="draft",
                        user_id=user.id, organization_id=org.id,
                        conversation_visibility="public", conversation_share_token=token)
        db.add(report)
        await db.flush()

        u = Completion(prompt={"content": f"Look up {email} and {EMP_ID}; plain question"},
                       completion=None, status="success", role="user",
                       report_id=report.id, user_id=user.id, turn_index=0)
        s = Completion(prompt=None, completion={"content": "ok"}, status="success",
                       role="system", report_id=report.id, turn_index=0)
        db.add_all([u, s])
        await db.flush()
        ae = AgentExecution(completion_id=s.id, organization_id=org.id, user_id=user.id,
                            report_id=report.id, status="success")
        db.add(ae)
        await db.flush()
        pd = PlanDecision(agent_execution_id=ae.id, seq=0, loop_index=0, plan_type="action", reasoning=f"Found {EMP_ID}",
                          assistant=f"Contact {email}", final_answer=f"Answer for {email}",
                          analysis_complete=True)
        db.add(pd)
        await db.flush()
        db.add(CompletionBlock(completion_id=s.id, agent_execution_id=ae.id, plan_decision_id=pd.id,
                               block_index=0, status="completed", source_type="decision", title="answer",
                               content=f"{EMP_ID} belongs to {email}. Revenue grew.",
                               reasoning=f"{email} matched"))
        await db.commit()
        return {"token": token, "email": email, "org_id": str(org.id)}


async def _fetch_public_conv(token: str) -> dict:
    async with async_session_maker() as db:
        return await ReportService().get_public_conversation(db, token)


@pytest.mark.asyncio
async def test_shared_conversation_is_masked_by_the_same_redactor_as_the_owner_view():
    from app.ai.llm.pii.loader import load_redactor_for_org

    seeded = await _seed_conversation()
    resp = await _fetch_public_conv(seeded["token"])
    blob = json.dumps(resp)
    assert seeded["email"] not in blob, "raw email leaked to anonymous share viewer"
    assert EMP_ID not in blob, "custom PII rule not applied on the share page"

    # Parity: each served text equals what the owner-path redactor produces.
    redactor = await load_redactor_for_org(seeded["org_id"], async_session_maker)
    assert redactor is not None
    user_c = next(c for c in resp["completions"] if c["role"] == "user")
    assert user_c["prompt"]["content"] == redactor.redact_display(
        f"Look up {seeded['email']} and {EMP_ID}; plain question")
    block = next(c for c in resp["completions"] if c["role"] == "system")["completion_blocks"][0]
    assert block["content"] == redactor.redact_display(f"{EMP_ID} belongs to {seeded['email']}. Revenue grew.")
    # Non-PII text still renders.
    assert "plain question" in user_c["prompt"]["content"]
    assert "Revenue grew." in block["content"]


@pytest.mark.asyncio
async def test_shared_conversation_is_unchanged_when_pii_protection_is_off():
    seeded = await _seed_conversation(pii_enabled=False)
    resp = await _fetch_public_conv(seeded["token"])
    user_c = next(c for c in resp["completions"] if c["role"] == "user")
    assert seeded["email"] in user_c["prompt"]["content"]
    block = next(c for c in resp["completions"] if c["role"] == "system")["completion_blocks"][0]
    assert EMP_ID in block["content"]
