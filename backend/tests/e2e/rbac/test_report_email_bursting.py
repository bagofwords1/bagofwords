"""Report emails are rendered per recipient when the report is identity-scoped.

A scheduled-results email, a "share by email" and a notify/send_email
attachment used to be rendered ONCE from the owner's stored rows and sent to
everyone. For an identity-scoped query (``source: identity`` params — "my
accounts") those rows are the owner's slice, so every recipient received the
owner's private rows, including addresses outside the organization.

Invariants pinned here (services/report_bursting.py):

  * each org member who can open the report receives THEIR rows — the
    identity-scoped step is executed as them (the live app's viewer run) and
    the copy is rendered with them as the viewer;
  * the owner's copy is the stored snapshot, unchanged;
  * an address with no account in the org, and a member who cannot open the
    report, receive nothing — and the owner is told who was skipped;
  * one recipient failing does not stop the others;
  * a deck is re-run from its stored slide code over the recipient's rows;
  * a report with NO identity-scoped content is mailed exactly as before:
    one message, every recipient (external included), the stored rows.

Boundaries stubbed: the SMTP client (settings.email_client) and the headless
renderers (Chromium for dashboards, LibreOffice for decks). The renderer stub
writes the payload it was handed into the "PDF", so an attachment's bytes are
exactly the rows that file would contain.
"""
import asyncio
import io
import json
import re
import uuid
from datetime import datetime, timedelta

import pytest

from app.dependencies import async_session_maker
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.visualization import Visualization
from app.models.widget import Widget
from tests.fixtures.artifact import seed_artifact

pytestmark = pytest.mark.e2e


def _run(coro):
    return asyncio.run(coro)


def _headers(token, org_id=None):
    h = {"Authorization": f"Bearer {token}"}
    if org_id:
        h["X-Organization-Id"] = str(org_id)
    return h


IDENTITY_PARAM = {
    "name": "Viewer", "type": "string", "label": "Viewer", "default": None,
    "required": False, "source": "identity", "identity_binding": "viewer.email",
    "options": None, "options_source": None, "strict_options": False,
}

# Each rep's accounts. The step filters by the identity param, so whoever runs
# it gets exactly their own rows — no data source needed.
ROWS_CODE = """
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    book = {book!r}
    reps = [r for r, accts in book.items() for _ in accts]
    accts = [a for _, accts in book.items() for a in accts]
    df = pd.DataFrame({{"rep": reps, "account": accts}})
    return df[df["rep"] == params["Viewer"]].reset_index(drop=True)
"""

PLAIN_CODE = """
def generate_df(ds_clients, excel_files):
    import pandas as pd
    return pd.DataFrame({"region": ["EMEA", "APAC"], "revenue": [10, 20]})
"""

DECK_CODE = """
prs = Presentation()
slide = prs.slides.add_slide(prs.slide_layouts[6])
box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(4))
box.text_frame.text = "ACCOUNTS:" + ",".join(str(r["account"]) for r in visualizations[0]["rows"])
prs.save(_pptx_output_path)
"""

SUMMARY = "OWNER-NARRATIVE: your top account is OWNER-ACCT-1"


# ── seeding ───────────────────────────────────────────────────────────────────

def _cast(test_client, create_report, bootstrap_admin, invite_user_to_org):
    admin = bootstrap_admin()
    org_id = admin["org_id"]
    owner = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    a = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    b = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    outsider_member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    report = create_report(
        title=f"Accounts {uuid.uuid4().hex[:6]}", user_token=owner["token"],
        org_id=org_id, data_sources=[],
    )
    return {"org_id": org_id, "owner": owner, "a": a, "b": b, "c": outsider_member,
            "report_id": report["id"], "external": f"partner-{uuid.uuid4().hex[:6]}@outside.example"}


async def _seed_graph(report_id, *, identity: bool, book: dict, mode: str = "page"):
    """One visualization over a step whose snapshot holds the OWNER's rows.

    Steps/visualizations/artifacts come from the agent in production and have
    no CRUD API — seeded directly, like the other report-rerun tests."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        owner_email = None
        from app.models.user import User
        owner_email = (await db.get(User, report.user_id)).email
        widget = Widget(title="Accounts", slug=f"w-{suffix}", report_id=report_id)
        db.add(widget)
        await db.flush()
        query = Query(
            title="My accounts", report_id=report_id, widget_id=widget.id,
            organization_id=report.organization_id, user_id=report.user_id,
            parameters=[IDENTITY_PARAM] if identity else [],
        )
        db.add(query)
        await db.flush()
        if identity:
            snapshot = {"rows": [{"rep": owner_email, "account": a} for a in book[owner_email]],
                        "columns": [{"field": "rep"}, {"field": "account"}]}
            code = ROWS_CODE.format(book=book)
        else:
            snapshot = {"rows": [{"region": "EMEA", "revenue": 10}, {"region": "APAC", "revenue": 20}],
                        "columns": [{"field": "region"}, {"field": "revenue"}]}
            code = PLAIN_CODE
        step = Step(
            title="Accounts", slug=f"s-{suffix}", status="success", widget_id=widget.id,
            query_id=query.id, code=code, data=snapshot,
            applied_params={"Viewer": owner_email} if identity else None,
            created_at=datetime.utcnow() - timedelta(hours=1),
        )
        db.add(step)
        await db.flush()
        query.default_step_id = step.id
        viz = Visualization(title="Accounts", status="success", report_id=report_id,
                            query_id=query.id, view={"type": "table"})
        db.add(viz)
        await db.flush()
        artifact = await seed_artifact(
            db, report_id=report_id, user_id=report.user_id,
            organization_id=report.organization_id, mode=mode, title="Accounts",
            content={"code": DECK_CODE if mode == "slides" else "function App() {}",
                     "visualization_ids": [str(viz.id)]},
        )
        await db.commit()
        return {"query_id": str(query.id), "viz_id": str(viz.id),
                "step_id": str(step.id), "artifact_id": str(artifact.id)}


def _book(cast):
    return {
        cast["owner"]["email"]: ["OWNER-ACCT-1", "OWNER-ACCT-2"],
        cast["a"]["email"]: ["A-ACCT-1"],
        cast["b"]["email"]: ["B-ACCT-1", "B-ACCT-2", "B-ACCT-3"],
        cast["c"]["email"]: ["C-ACCT-1"],
    }


def _share(test_client, cast, user_keys=("a", "b")):
    resp = test_client.put(
        f"/api/reports/{cast['report_id']}/visibility/artifact",
        json={"visibility": "shared",
              "shared_user_ids": [cast[k]["user_id"] for k in user_keys]},
        headers=_headers(cast["owner"]["token"], cast["org_id"]),
    )
    assert resp.status_code == 200, resp.text


# ── boundaries ────────────────────────────────────────────────────────────────

class FakeMail:
    """settings.email_client stand-in: records every message at send time."""

    def __init__(self):
        self.sent = []
        self.refuse = set()

    async def send_message(self, message):
        to = [getattr(r, "email", str(r)) for r in message.recipients]
        if self.refuse & set(to):
            raise RuntimeError("550 mailbox unavailable")
        atts = []
        for item in message.attachments or []:
            upload, meta = item if isinstance(item, tuple) else (item, {})
            upload.file.seek(0)
            atts.append(upload.file.read())
        self.sent.append({"to": to, "subject": message.subject, "body": str(message.body),
                          "attachments": atts})

    @property
    def reports(self):
        """Report mail only (seeding sends welcome emails too)."""
        return [m for m in self.sent if "Accounts" in (m["subject"] or "")]

    def to(self, email):
        return [m for m in self.reports if email in m["to"]]


@pytest.fixture
def mail(monkeypatch):
    from app.settings.config import settings

    fake = FakeMail()
    monkeypatch.setattr(settings, "email_client", fake)
    return fake


@pytest.fixture
def renderers(monkeypatch, tmp_path):
    """Stub Chromium/LibreOffice: a 'PDF' is the JSON rows it was handed."""
    import app.data_sources.clients._office_convert as oc
    import app.services.report_pdf_service as pdf_mod
    from app.services.report_pdf_service import ReportPdfService

    monkeypatch.setattr(ReportPdfService, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(ReportPdfService, "PPTX_DIR", tmp_path)
    monkeypatch.setattr(pdf_mod, "get_inline_scripts", lambda mode="page": "")

    async def fake_generate_pdf(self, artifact_id, html_content, mode="page"):
        m = re.search(r"window\.ARTIFACT_DATA\s*=\s*(\{.*?\});", html_content, re.S)
        payload = json.loads(m.group(1))
        rows = [r for v in payload["visualizations"] for r in v["rows"]]
        (tmp_path / f"{artifact_id}.pdf").write_text(json.dumps(rows))
        return f"pdfs/{artifact_id}.pdf"

    monkeypatch.setattr(ReportPdfService, "generate_pdf", fake_generate_pdf)
    # LibreOffice: hand the deck back unchanged, so the "PDF" is the .pptx.
    monkeypatch.setattr(oc, "office_to_pdf_bytes", lambda raw, name: raw)
    return tmp_path


def _pdf_accounts(blob: bytes) -> set:
    return {r.get("account") or r.get("region") for r in json.loads(blob.decode())}


def _deck_text(blob: bytes) -> str:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(blob))
    return " ".join(sh.text_frame.text for s in prs.slides for sh in s.shapes if sh.has_text_frame)


def _subscribers(cast, *, users=("owner", "a", "b", "c"), external=True):
    subs = [{"type": "user", "id": cast[k]["user_id"]} for k in users]
    if external:
        subs.append({"type": "email", "address": cast["external"]})
    return subs


async def _send_scheduled(cast, subscribers, artifacts=1):
    from app.services.notification_service import notification_service

    await notification_service.send_scheduled_prompt_results(
        report_id=cast["report_id"], report_title="Accounts", subscribers=subscribers,
        report_url=f"http://localhost:3000/reports/{cast['report_id']}",
        exec_summary={"iterations": 1, "queries": 1, "artifacts": artifacts,
                      "last_content": SUMMARY},
        locale="en",
    )


async def _owner_inbox(cast):
    from sqlalchemy import select
    from app.models.notification import Notification

    async with async_session_maker() as db:
        return list((await db.execute(
            select(Notification).where(
                Notification.user_id == cast["owner"]["user_id"],
                Notification.type == "delivery_skipped",
            )
        )).scalars().all())


# ── scheduled results ─────────────────────────────────────────────────────────

def test_scheduled_results_give_each_member_their_own_rows(
    test_client, create_report, bootstrap_admin, invite_user_to_org, mail, renderers,
):
    cast = _cast(test_client, create_report, bootstrap_admin, invite_user_to_org)
    book = _book(cast)
    _run(_seed_graph(cast["report_id"], identity=True, book=book))
    _share(test_client, cast)  # a and b can open it; c is a member who cannot

    _run(_send_scheduled(cast, _subscribers(cast)))

    for key in ("a", "b"):
        msgs = mail.to(cast[key]["email"])
        assert len(msgs) == 1, f"{key} gets exactly one, personal, message"
        assert msgs[0]["to"] == [cast[key]["email"]], "a personal copy is addressed to one person"
        assert len(msgs[0]["attachments"]) == 1
        assert _pdf_accounts(msgs[0]["attachments"][0]) == set(book[cast[key]["email"]])
        # The run's written summary was drawn from the owner's rows.
        assert "OWNER-NARRATIVE" not in msgs[0]["body"]

    owner_msgs = mail.to(cast["owner"]["email"])
    assert len(owner_msgs) == 1
    assert _pdf_accounts(owner_msgs[0]["attachments"][0]) == set(book[cast["owner"]["email"]])
    assert "OWNER-NARRATIVE" in owner_msgs[0]["body"], "the owner's copy is unchanged"

    # No account in the org, or no access to the report: nothing is sent.
    assert mail.to(cast["external"]) == []
    assert mail.to(cast["c"]["email"]) == []
    # Nobody but the owner ever receives an owner row.
    for m in mail.reports:
        if cast["owner"]["email"] not in m["to"]:
            for blob in m["attachments"]:
                assert not any(str(a).startswith("OWNER") for a in _pdf_accounts(blob))

    inbox = _run(_owner_inbox(cast))
    assert inbox, "the owner is told who was skipped"
    undelivered = {u["email"] for u in inbox[0].subject_json["undelivered"]}
    assert undelivered == {cast["external"], cast["c"]["email"]}


def test_scheduled_results_keep_going_when_one_recipient_fails(
    test_client, create_report, bootstrap_admin, invite_user_to_org, mail, renderers,
):
    cast = _cast(test_client, create_report, bootstrap_admin, invite_user_to_org)
    book = _book(cast)
    _run(_seed_graph(cast["report_id"], identity=True, book=book))
    _share(test_client, cast)
    mail.refuse = {cast["a"]["email"]}

    _run(_send_scheduled(cast, _subscribers(cast, users=("a", "b"), external=False)))

    assert mail.to(cast["a"]["email"]) == []
    b_msgs = mail.to(cast["b"]["email"])
    assert len(b_msgs) == 1
    assert _pdf_accounts(b_msgs[0]["attachments"][0]) == set(book[cast["b"]["email"]])
    inbox = _run(_owner_inbox(cast))
    assert {u["email"]: u["status"] for u in inbox[0].subject_json["undelivered"]} == {
        cast["a"]["email"]: "failed"}


def test_scheduled_results_for_a_plain_report_are_unchanged(
    test_client, create_report, bootstrap_admin, invite_user_to_org, mail, renderers,
):
    """No identity-scoped content: one message, every recipient, stored rows,
    no per-recipient query runs."""
    from sqlalchemy import func, select
    from app.models.step_user_result import StepUserResult

    cast = _cast(test_client, create_report, bootstrap_admin, invite_user_to_org)
    seeded = _run(_seed_graph(cast["report_id"], identity=False, book=_book(cast)))

    _run(_send_scheduled(cast, _subscribers(cast)))

    assert len(mail.reports) == 1, "a plain report is still a single send"
    msg = mail.reports[0]
    assert set(msg["to"]) == {cast[k]["email"] for k in ("owner", "a", "b", "c")} | {cast["external"]}
    assert _pdf_accounts(msg["attachments"][0]) == {"EMEA", "APAC"}
    assert "OWNER-NARRATIVE" in msg["body"]

    async def _viewer_runs():
        async with async_session_maker() as db:
            return (await db.execute(select(func.count(StepUserResult.id)).where(
                StepUserResult.step_id == seeded["step_id"]))).scalar()
    assert _run(_viewer_runs()) == 0
    assert _run(_owner_inbox(cast)) == []


# ── share by email ───────────────────────────────────────────────────────────

def _notify(test_client, cast, recipients):
    return test_client.post(
        f"/api/reports/{cast['report_id']}/notify",
        json={"type": "share_dashboard", "channels": ["email"], "recipients": recipients,
              "share_url": f"http://localhost:3000/r/{cast['report_id']}"},
        headers=_headers(cast["owner"]["token"], cast["org_id"]),
    )


def test_share_email_of_an_identity_dashboard_is_personal_per_member(
    test_client, create_report, bootstrap_admin, invite_user_to_org, mail, renderers,
):
    cast = _cast(test_client, create_report, bootstrap_admin, invite_user_to_org)
    book = _book(cast)
    _run(_seed_graph(cast["report_id"], identity=True, book=book))
    _share(test_client, cast)

    resp = _notify(test_client, cast, [cast["a"]["email"], cast["b"]["email"], cast["external"]])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {r for d in body["dispatched"] for r in d["recipients"]} == {cast["a"]["email"], cast["b"]["email"]}
    assert {r for d in body["skipped"] for r in d["recipients"]} == {cast["external"]}

    for key in ("a", "b"):
        (msg,) = mail.to(cast[key]["email"])
        assert _pdf_accounts(msg["attachments"][0]) == set(book[cast[key]["email"]])
    assert mail.to(cast["external"]) == []


def test_share_email_to_only_skipped_recipients_reports_an_error(
    test_client, create_report, bootstrap_admin, invite_user_to_org, mail, renderers,
):
    cast = _cast(test_client, create_report, bootstrap_admin, invite_user_to_org)
    _run(_seed_graph(cast["report_id"], identity=True, book=_book(cast)))

    resp = _notify(test_client, cast, [cast["external"]])
    assert resp.status_code == 200, resp.text
    assert resp.json()["dispatched"] == []
    assert resp.json()["errors"], "the dialog must not report 'sent' for a mail nobody got"
    assert mail.reports == []


def test_share_email_of_a_plain_dashboard_is_unchanged(
    test_client, create_report, bootstrap_admin, invite_user_to_org, mail, renderers,
):
    cast = _cast(test_client, create_report, bootstrap_admin, invite_user_to_org)
    _run(_seed_graph(cast["report_id"], identity=False, book=_book(cast)))

    resp = _notify(test_client, cast, [cast["a"]["email"], cast["external"]])
    assert resp.status_code == 200, resp.text
    assert resp.json()["errors"] == [] and resp.json()["skipped"] == []
    assert len(mail.reports) == 1
    assert set(mail.reports[0]["to"]) == {cast["a"]["email"], cast["external"]}
    assert _pdf_accounts(mail.reports[0]["attachments"][0]) == {"EMEA", "APAC"}


# ── decks ─────────────────────────────────────────────────────────────────────

def test_identity_deck_is_rerun_from_its_stored_code_per_member(
    test_client, create_report, bootstrap_admin, invite_user_to_org, mail, renderers,
):
    """A deck is stored slide code + rows; each member's copy re-executes that
    code over their rows instead of shipping the deck baked for the owner."""
    cast = _cast(test_client, create_report, bootstrap_admin, invite_user_to_org)
    book = _book(cast)
    seeded = _run(_seed_graph(cast["report_id"], identity=True, book=book, mode="slides"))
    # The deck generation wrote for the owner.
    (renderers / f"{seeded['artifact_id']}.pptx").write_bytes(_owner_deck_bytes(book, cast))
    _share(test_client, cast)

    _run(_send_scheduled(cast, _subscribers(cast, users=("owner", "a", "b"), external=False)))

    for key in ("a", "b"):
        (msg,) = mail.to(cast[key]["email"])
        text = _deck_text(msg["attachments"][0])
        assert all(acct in text for acct in book[cast[key]["email"]])
        assert "OWNER-ACCT" not in text
    (owner_msg,) = mail.to(cast["owner"]["email"])
    assert "OWNER-ACCT-1" in _deck_text(owner_msg["attachments"][0])


def _owner_deck_bytes(book, cast) -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(4))
    box.text_frame.text = "ACCOUNTS:" + ",".join(book[cast["owner"]["email"]])
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def test_public_deck_download_for_a_member_with_own_rows_is_not_the_owners(
    test_client, create_report, bootstrap_admin, invite_user_to_org, renderers,
):
    """A signed-in member who ran the identity query resolves to their OWN rows;
    the deck baked for the owner must not be served to them."""
    cast = _cast(test_client, create_report, bootstrap_admin, invite_user_to_org)
    book = _book(cast)
    seeded = _run(_seed_graph(cast["report_id"], identity=True, book=book, mode="slides"))
    (renderers / f"{seeded['artifact_id']}.pptx").write_bytes(_owner_deck_bytes(book, cast))
    _share(test_client, cast)

    run = test_client.post(f"/api/r/{cast['report_id']}/run", headers=_headers(cast["a"]["token"]))
    assert run.status_code == 200, run.text

    resp = test_client.get(f"/api/r/{cast['report_id']}/export_pptx",
                           headers=_headers(cast["a"]["token"]))
    assert resp.status_code == 200, resp.text
    text = _deck_text(resp.content)
    assert "A-ACCT-1" in text and "OWNER-ACCT" not in text


# ── notify / send_email attachments ──────────────────────────────────────────

def test_notify_attachment_of_identity_rows_is_rendered_per_recipient(
    test_client, create_report, bootstrap_admin, invite_user_to_org, mail, renderers,
):
    from app.ai.tools.schemas.send_email import EmailAttachmentSpec
    from app.models.organization import Organization
    from app.models.user import User
    from app.services.notify_service import notify_service

    cast = _cast(test_client, create_report, bootstrap_admin, invite_user_to_org)
    book = _book(cast)
    seeded = _run(_seed_graph(cast["report_id"], identity=True, book=book))
    _share(test_client, cast)

    async def _go():
        async with async_session_maker() as db:
            sender = await db.get(User, cast["owner"]["user_id"])
            org = await db.get(Organization, cast["org_id"])
            report = await db.get(Report, cast["report_id"])
            return await notify_service.notify(
                db, sender=sender, organization=org, report=report,
                subject="Accounts", body="Attached.",
                attachment_specs=[EmailAttachmentSpec(ref_type="visualization",
                                                      ref_id=seeded["viz_id"], format="csv")],
                recipient_emails=[cast["b"]["email"], cast["c"]["email"]],
            )
    _run(_go())

    def _csv_accounts(email):
        import csv

        (msg,) = mail.to(email)
        if not msg["attachments"]:
            return set()
        rows = list(csv.DictReader(io.StringIO(msg["attachments"][0].decode("utf-8-sig"))))
        return {r.get("account") for r in rows}

    assert _csv_accounts(cast["b"]["email"]) == set(book[cast["b"]["email"]])
    # c is a member who cannot open the report: the message, but no rows.
    assert _csv_accounts(cast["c"]["email"]) == set()
    assert mail.to(cast["c"]["email"]), "the message itself still reaches a member"
    # The sender (owner) keeps the stored snapshot.
    assert _csv_accounts(cast["owner"]["email"]) == set(book[cast["owner"]["email"]])
