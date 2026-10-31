"""browser_extract keeps a long page's full text instead of dropping the overflow.

Contracts:
- A page longer than max_chars returns a preview of exactly max_chars plus a
  text_file_id; the saved file holds the page's complete text, it is linked to
  the report, and read_file can page through it in the same turn.
- A page that fits inline writes no file.
- Extracting the same unchanged page again reuses the saved file.

The report, user and browser connection are seeded through the API; the
browser page is the only stubbed boundary (no real Chromium or network).
"""
import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

pytestmark = pytest.mark.e2e

ALLOWED = "https://allowed.example.test"


def _page_text(n_chars: int) -> str:
    # Numbered lines so any dropped or duplicated window is visible.
    lines, size, i = [], 0, 0
    while size < n_chars:
        line = f"row {i:05d} | région {i % 7} | value {i * 13}"
        lines.append(line)
        size += len(line) + 1
        i += 1
    return "\n".join(lines)[:n_chars]


class _FakeLocator:
    def __init__(self, page):
        self._page = page

    async def inner_text(self, **_kwargs):
        return self._page.body


class _FakePage:
    def __init__(self, url, title, body):
        self.url, self._title, self.body = url, title, body

    async def title(self):
        return self._title

    def locator(self, _selector):
        return _FakeLocator(self)


def _owner_with_browser(create_user, login_user, whoami, create_report, create_connection,
                        create_domain_from_connection):
    user = create_user(email=f"extract-{uuid.uuid4().hex[:10]}@example.com")
    token = login_user(user["email"], user["password"])
    identity = whoami(token)
    org_id = identity["organizations"][0]["id"]
    conn = create_connection(
        name=f"Browser {uuid.uuid4().hex[:8]}", type="browser",
        config={"url_patterns": [f"{ALLOWED}/**"], "allow_downloads": False},
        credentials={}, user_token=token, org_id=org_id,
    )
    source = create_domain_from_connection(
        name=f"Browser source {uuid.uuid4().hex[:8]}", connection_id=conn["id"],
        user_token=token, org_id=org_id, is_public=True,
    )
    report = create_report(title="Extract", user_token=token, org_id=org_id, data_sources=[source["id"]])
    return report["id"], identity["id"], org_id, token


async def _extract_all(report_id, user_id, org_id, pages):
    """Run browser_extract for each (body, max_chars) like one agent turn, then
    page through any saved file with read_file. Returns per-page results."""
    from app.ai.tools.implementations._browser_common import BrowserSession, session_manager
    from app.ai.tools.implementations.browser_extract import BrowserExtractTool
    from app.ai.tools.implementations.read_file import ReadFileTool
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User

    async with async_session_maker() as db:
        report = (await db.execute(
            select(Report).options(selectinload(Report.files)).where(Report.id == report_id)
        )).scalar_one()
        ctx = {"db": db, "report": report, "user": await db.get(User, user_id),
               "organization": await db.get(Organization, org_id), "excel_files": []}

        results = []
        for body, max_chars in pages:
            sid = f"test-{uuid.uuid4().hex[:8]}"
            session = BrowserSession(sid, [f"{ALLOWED}/**"], False)
            session.page = _FakePage(f"{ALLOWED}/report", "Quarterly report — EMEA", body)
            session.scope = session_manager.scope_for(ctx, internal=False)
            session_manager._sessions[sid] = session
            try:
                events = [e async for e in BrowserExtractTool().run_stream(
                    {"session_id": sid, "max_chars": max_chars}, ctx)]
            finally:
                session_manager._sessions.pop(sid, None)
            out = events[-1].payload["output"]

            read_back = None
            if out.get("text_file_id"):
                parts, offset = [], 0
                while offset is not None:
                    ev = [e async for e in ReadFileTool().run_stream(
                        {"file_id": out["text_file_id"], "offset": offset, "length": 7000}, ctx)]
                    window = ev[-1].payload["output"]
                    assert window.get("success", True), window
                    parts.append(window["text"])
                    offset = window.get("next_cursor")
                read_back = "".join(parts)
            results.append((out, read_back))
        await db.commit()
        return results


async def _report_file_ids(report_id):
    from app.dependencies import async_session_maker
    from app.models.report import Report

    async with async_session_maker() as db:
        report = (await db.execute(
            select(Report).options(selectinload(Report.files)).where(Report.id == report_id)
        )).scalar_one()
        return {str(f.id) for f in report.files}


@pytest.mark.parametrize("total,max_chars", [(23_457, 1_200), (61_003, 12_000), (140_009, 60_000)])
def test_long_page_full_text_is_saved_and_readable(
    total, max_chars, create_user, login_user, whoami, create_report,
    create_connection, create_domain_from_connection,
):
    report_id, user_id, org_id, _ = _owner_with_browser(
        create_user, login_user, whoami, create_report, create_connection, create_domain_from_connection)
    body = _page_text(total)

    [(out, read_back)] = asyncio.run(_extract_all(report_id, user_id, org_id, [(body, max_chars)]))

    assert out["success"] is True
    assert out["truncated"] is True
    assert out["total_chars"] == total
    assert out["text"].startswith(body[:max_chars])
    assert body[max_chars:max_chars + 50] not in out["text"]  # inline is only the preview
    assert out["text_file_id"]
    assert read_back == body  # nothing past the inline limit is lost
    assert out["text_file_id"] in asyncio.run(_report_file_ids(report_id))


def test_page_that_fits_inline_writes_no_file(
    create_user, login_user, whoami, create_report, create_connection, create_domain_from_connection,
):
    report_id, user_id, org_id, _ = _owner_with_browser(
        create_user, login_user, whoami, create_report, create_connection, create_domain_from_connection)
    before = asyncio.run(_report_file_ids(report_id))
    body = _page_text(4_321)

    [(out, _)] = asyncio.run(_extract_all(report_id, user_id, org_id, [(body, 5_000)]))

    assert out["truncated"] is False
    assert out["text"] == body
    assert out["text_file_id"] is None
    assert asyncio.run(_report_file_ids(report_id)) == before


def test_re_extracting_an_unchanged_page_reuses_the_file_and_a_changed_page_does_not(
    create_user, login_user, whoami, create_report, create_connection, create_domain_from_connection,
):
    report_id, user_id, org_id, _ = _owner_with_browser(
        create_user, login_user, whoami, create_report, create_connection, create_domain_from_connection)
    body = _page_text(30_011)
    changed = body + "\nrow appended later"

    (a, _), (b, _), (c, read_c) = asyncio.run(_extract_all(
        report_id, user_id, org_id, [(body, 2_000), (body, 2_000), (changed, 2_000)]))

    assert a["text_file_id"] == b["text_file_id"]
    assert c["text_file_id"] != a["text_file_id"]
    assert read_c == changed
    assert {a["text_file_id"], c["text_file_id"]} <= asyncio.run(_report_file_ids(report_id))
