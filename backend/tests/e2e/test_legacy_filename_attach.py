"""Repro + regression guard: a filename the OS handed us as raw bytes must be
storable — and a failed attach must not take the rest of the turn down with it.

From the field (a Hebrew mortgage share, cp862/DOS-Hebrew filenames):

    invalid input for query argument $1: '\\udc84\\udc8b\\udc90...'
    ('utf-8' codec can't encode characters in position 0-5: surrogates not
     allowed)
    ... This Session's transaction has been rolled back due to a previous
    exception during flush.

`files.filename` / `files.path` are UTF-8 columns; a name carrying
surrogateescape'd bytes is rejected by the driver *during flush*, which leaves
the AsyncSession unusable. Nobody rolled it back, so every later statement in
that agent turn failed too — one un-storable filename ended the whole run.

Both invariants are asserted against the real DB (sqlite and postgres reject
lone surrogates identically — the driver encodes to UTF-8 either way):

  1. any name, whatever encoding it arrived in, persists and round-trips;
  2. if a write fails anyway, the session is left usable.

Companion unit tests (recovery at the connector boundary, `storage_safe_name`
itself): tests/unit/test_legacy_filename_persistence.py

Run:
    cd backend
    uv run pytest tests/e2e/test_legacy_filename_attach.py -v
"""
import asyncio
import io
import os
import uuid

import pytest
from fastapi import UploadFile
from sqlalchemy import select
from starlette.datastructures import Headers

from app.dependencies import async_session_maker
from app.models.file import File
from app.models.report import Report

# A real name off the reported share, in the encoding the share stores it in.
HEB_NAME = "הכנסות לווים + דפי חשבון בנק.pdf"
SURROGATE_NAME = HEB_NAME.encode("cp862").decode("utf-8", "surrogateescape")
# A second, differently-encoded name: the fix must not be cp862-specific.
CP1255_NAME = "דוח משכנתא 2024.pdf"
CP1255_SURROGATE = CP1255_NAME.encode("cp1255").decode("utf-8", "surrogateescape")
PDF_BYTES = b"%PDF-1.4\n(scanned page, no text layer)\n%%EOF\n"


def _run(coro):
    return asyncio.run(coro)


def _storable(s: str) -> bool:
    """Exactly what the DB driver does to a string parameter before the wire."""
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return "\x00" not in s


def _org_and_user(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    me = whoami(token)
    return me["organizations"][0]["id"], me["id"]


async def _seed_report(org_id, user_id):
    """A report to attach to. Created directly: the attach helper takes model
    objects out of the agent's runtime context, not an HTTP payload."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = Report(
            title=f"Legacy names {suffix}",
            slug=f"legacy-names-{suffix}",
            user_id=user_id,
            organization_id=org_id,
            status="draft",
        )
        db.add(report)
        await db.commit()
        return str(report.id)


async def _attach(report_id, org_id, user_id, *, filename, source_ref=None):
    """Drive the real attach helper on a real session, then keep using that
    session — a poisoned session shows up here, not in a mock."""
    from app.ai.tools.implementations._file_tool_common import attach_drive_file_to_session
    from app.models.organization import Organization
    from app.models.user import User

    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        file_id = await attach_drive_file_to_session(
            {
                "db": db,
                "report": report,
                "user": await db.get(User, user_id),
                "organization": await db.get(Organization, org_id),
                "excel_files": [],
            },
            filename=filename,
            content_bytes=PDF_BYTES,
            mime_type="application/pdf",
            connection_id=str(uuid.uuid4()),
            source_ref=source_ref or filename,
        )
        # The session must still work whether or not the attach succeeded.
        still_usable = (await db.execute(select(Report.id).where(Report.id == report_id))).first()
        return file_id, bool(still_usable)


async def _load_file(file_id):
    async with async_session_maker() as db:
        return await db.get(File, file_id)


@pytest.mark.e2e
@pytest.mark.parametrize(
    "raw_name,expected",
    [
        (SURROGATE_NAME, HEB_NAME),        # cp862 — the reported case
        (CP1255_SURROGATE, CP1255_NAME),   # cp1255 — same class, other charset
        ("quarterly report.pdf", "quarterly report.pdf"),   # clean: untouched
    ],
)
def test_attaching_a_legacy_named_file_persists_and_round_trips(
    create_user, login_user, whoami, raw_name, expected
):
    org_id, user_id = _org_and_user(create_user, login_user, whoami)
    report_id = _run(_seed_report(org_id, user_id))

    file_id, session_usable = _run(_attach(report_id, org_id, user_id, filename=raw_name))

    assert file_id, "attach failed — the INSERT never got past flush"
    assert session_usable, "session left needing a rollback; the rest of the turn dies"

    row = _run(_load_file(file_id))
    assert row.filename == expected
    # The DB path and the bytes on disk must name the same file — the scrub has
    # to happen before the storage path is derived, not after.
    assert _storable(row.path)
    assert os.path.exists(row.path), f"row.path {row.path!r} has no file behind it"
    with open(row.path, "rb") as fh:
        assert fh.read() == PDF_BYTES
    os.remove(row.path)


@pytest.mark.e2e
def test_a_failed_attach_leaves_the_session_usable(create_user, login_user, whoami, monkeypatch):
    """Defence in depth: the sanitizer covers the names we know about, but any
    write can fail (constraint, driver, disk). Whatever the cause, the attach is
    best-effort — it must not strand the session mid-transaction."""
    import app.ai.tools.implementations._file_tool_common as ftc

    # Simulate a string that still slips past the sanitizer: the driver then
    # fails the flush exactly as it did for the reported filename.
    monkeypatch.setattr(ftc, "storage_safe_name", lambda s: s)

    org_id, user_id = _org_and_user(create_user, login_user, whoami)
    report_id = _run(_seed_report(org_id, user_id))

    file_id, session_usable = _run(_attach(report_id, org_id, user_id, filename=SURROGATE_NAME))

    assert file_id is None, "an unstorable name must not report success"
    assert session_usable, "session left needing a rollback; the rest of the turn dies"


@pytest.mark.e2e
def test_uploading_a_legacy_named_file_persists_and_round_trips(
    create_user, login_user, whoami
):
    """Same guarantee on the user-upload path: the multipart filename is
    attacker/OS-controlled and lands in the same UTF-8 columns."""
    from app.services.file_service import FileService

    org_id, user_id = _org_and_user(create_user, login_user, whoami)

    async def scenario():
        from app.models.organization import Organization
        from app.models.user import User

        async with async_session_maker() as db:
            upload = UploadFile(
                file=io.BytesIO(PDF_BYTES), filename=SURROGATE_NAME,
                headers=Headers({"content-type": "application/pdf"}),
            )
            schema = await FileService().upload_file(
                db, upload, await db.get(User, user_id), await db.get(Organization, org_id),
            )
            usable = (await db.execute(select(File.id).where(File.id == schema.id))).first()
            return schema, bool(usable)

    schema, session_usable = _run(scenario())
    assert session_usable
    assert schema.filename == HEB_NAME

    row = _run(_load_file(schema.id))
    assert row.filename == HEB_NAME
    assert os.path.exists(row.path)
    os.remove(row.path)


@pytest.mark.e2e
def test_saving_a_legacy_named_attachment_persists_and_round_trips(
    create_user, login_user, whoami
):
    """Inbound email attachments land through `save_bytes_as_file`, and their
    names come off a MIME header — legacy codepages are routine there."""
    from app.services.file_service import FileService

    org_id, user_id = _org_and_user(create_user, login_user, whoami)

    async def scenario():
        from app.models.organization import Organization
        from app.models.user import User

        async with async_session_maker() as db:
            return await FileService().save_bytes_as_file(
                db, content=PDF_BYTES, filename=CP1255_SURROGATE,
                content_type="application/pdf",
                current_user=await db.get(User, user_id),
                organization=await db.get(Organization, org_id),
            )

    row = _run(scenario())
    assert row.filename == CP1255_NAME
    assert os.path.exists(row.path)
    os.remove(row.path)
