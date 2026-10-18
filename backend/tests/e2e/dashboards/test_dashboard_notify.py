"""Telling people a dashboard was shared with them (phase 8).

The share window's "notify" sends the email (and, to members, the in-app
notice) for the dashboard itself: the link is built by the server -- the
dashboard's /d link, by its name when it has one -- never taken from the
page. Only the owner sends it, and only for a published dashboard (sharing
waits for Publish).
"""
import pytest

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

SALES = "conv_three_dash/dash_sales"


@pytest.fixture
def sent(monkeypatch):
    from app.services import email_client_resolver
    from app.services.inbox_service import inbox_service
    from app.services.notification_service import notification_service

    out = {"dispatch": [], "inbox": []}

    async def dispatch(**kwargs):
        out["dispatch"].append(kwargs)
        return {"dispatched": [], "errors": []}

    async def inbox(db, **kwargs):
        out["inbox"].append(kwargs)
        return []

    async def available(*args, **kwargs):
        return True

    monkeypatch.setattr(notification_service, "dispatch", dispatch)
    monkeypatch.setattr(inbox_service, "notify_dashboard_share", inbox)
    monkeypatch.setattr(email_client_resolver, "is_outbound_available", available)
    return out


def _notify(test_client, corpus, aid, person="owner", **body):
    payload = {"channels": ["email"], "recipients": ["member@corpus.com", "cfo@outside.com"], **body}
    return test_client.post(f"/api/dashboards/{aid}/notify", json=payload, headers=as_(corpus, person))


@pytest.mark.e2e
def test_the_email_carries_the_dashboards_own_link(corpus, corpus_rows, test_client, sent):
    aid = own(corpus_rows, corpus, SALES)
    publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    resp = _notify(test_client, corpus, aid, share_url="https://evil.example/phish", message="Look")
    assert resp.status_code == 200, resp.text
    (mail,) = sent["dispatch"]
    assert mail["share_url"].endswith(f"/d/{aid}") and "evil" not in mail["share_url"]
    assert mail["notification_type"].value == "share_dashboard" and mail["message"] == "Look"
    assert mail["organization_id"] == corpus.cast.org_id
    (notice,) = sent["inbox"]  # members only: the outside address gets the email alone
    assert notice["user_ids"] == [corpus.cast.id("member")] and str(notice["artifact"].id) == aid

    named = test_client.put(f"/api/dashboards/{aid}/settings", json={"slug": "weekly-sales"},
                            headers=as_(corpus, "owner"))
    assert named.status_code == 200
    assert _notify(test_client, corpus, aid).status_code == 200
    assert sent["dispatch"][-1]["share_url"].endswith("/d/weekly-sales")


@pytest.mark.e2e
def test_only_the_owner_tells_and_only_once_published(corpus, corpus_rows, test_client, sent):
    aid = own(corpus_rows, corpus, SALES)
    early = _notify(test_client, corpus, aid)
    assert early.status_code == 409 and early.json()["error_code"] == "dashboard.share_requires_publish"
    publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    assert _notify(test_client, corpus, aid, person="member").status_code in (403, 404)
    assert sent["dispatch"] == []


def _attachments(mail):
    import asyncio

    return asyncio.run(mail["attachment_builder"]())


@pytest.mark.e2e
def test_the_email_attaches_the_published_versions_pdf_as_today(corpus, corpus_rows, test_client, sent,
                                                                 monkeypatch):
    """As the conversation's share email attaches its PDF: the release's,
    as a reader would export it from /d."""
    from pathlib import Path

    from app.services import dashboard_data
    from app.services.report_pdf_service import ReportPdfService

    rendered, viewers = [], []
    real_build = dashboard_data.ReleaseRender.build.__func__

    def build(cls, artifact, release, user):
        viewers.append(user)
        return real_build(cls, artifact, release, user)

    monkeypatch.setattr(dashboard_data.ReleaseRender, "build", classmethod(build))

    async def render(self, db, release_render):
        rendered.append(release_render)
        path = ReportPdfService.UPLOADS_DIR / f"notify-test-{len(rendered)}.pdf"
        path.write_bytes(b"%PDF-1.4")
        return str(path)

    monkeypatch.setattr(ReportPdfService, "generate_for_release", render)
    aid = own(corpus_rows, corpus, SALES)
    publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    assert _notify(test_client, corpus, aid).status_code == 200
    (attachment,) = _attachments(sent["dispatch"][-1])
    Path(attachment["file"]).unlink(missing_ok=True)
    assert attachment["filename"] == "לוח מכירות.pdf" and attachment["subtype"] == "pdf"
    assert len(rendered) == 1 and viewers == [None]  # a reader's view, never the owner's


@pytest.mark.e2e
def test_no_pdf_when_each_reader_sees_their_own_data(corpus, corpus_rows, test_client, sent, monkeypatch):
    from app.services.report_pdf_service import ReportPdfService

    async def render(self, db, release_render):
        raise AssertionError("no PDF of the owner's rows")

    monkeypatch.setattr(ReportPdfService, "generate_for_release", render)
    # A connection each person signs in to, run as the viewer: the stored
    # rows are the owner's, so no file carries them (as the /d export).
    aid = own(corpus_rows, corpus, "user_required/dash")
    publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    assert _notify(test_client, corpus, aid).status_code == 200
    assert _attachments(sent["dispatch"][-1]) == []


@pytest.mark.e2e
def test_the_attached_pdf_does_not_stay_on_disk(corpus, corpus_rows, test_client, sent, monkeypatch):
    """As the /d export removes its file once sent: the email's copy of the
    release's rows goes once the email went."""
    from pathlib import Path

    from app.services.notification_service import notification_service
    from app.services.report_pdf_service import ReportPdfService

    made = []

    async def render(self, db, release_render):
        path = ReportPdfService.UPLOADS_DIR / "notify-cleanup-test.pdf"
        path.write_bytes(b"%PDF-1.4")
        made.append(path)
        return str(path)

    async def dispatch(**kwargs):  # as the email channel does: builds the attachment, sends
        attachments = await kwargs["attachment_builder"]()
        assert attachments and Path(attachments[0]["file"]).exists()
        return {"dispatched": [], "errors": []}

    monkeypatch.setattr(ReportPdfService, "generate_for_release", render)
    monkeypatch.setattr(notification_service, "dispatch", dispatch)
    aid = own(corpus_rows, corpus, SALES)
    publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    assert _notify(test_client, corpus, aid).status_code == 200
    assert made and not made[0].exists()
