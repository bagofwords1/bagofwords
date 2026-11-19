"""<BowFile> references and the attachable-file lookup shared by create_artifact and edit_artifact."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai.tools.implementations import _artifact_files  # noqa: E402
from app.ai.tools.implementations._artifact_refs import (  # noqa: E402
    bow_file_literal_ids, file_reference_errors, normalize_file_id,
)

FID = "3f2a1b4c-5d6e-4f70-8a9b-0c1d2e3f4a5b"


@pytest.mark.parametrize("tag", [
    f'<BowFile id="{FID}" />',
    f"<BowFile className='h-8' id='{FID}'/>",
    f'<BowFile id={{"{FID}"}} />',
    f'<BowFile fit="cover" id={{`{FID}`}} />',
    # A ">" inside an earlier attribute must not end the tag.
    f'<BowFile onLoad={{() => setReady(true)}} id="{FID}" />',
    f'<BowFile alt="a > b" id="{FID}" />',
    # Text inside another attribute's value is not the id.
    f"<BowFile alt='id=\"photo\"' id=\"{FID}\" />",
    f'<BowFile title="file id=5" id="{FID}" />',
])
def test_a_literal_id_is_found_in_every_tag_shape(tag):
    assert bow_file_literal_ids(f"function App() {{ return <div>{tag}</div> }}") == {FID}


@pytest.mark.parametrize("tag", [
    "<BowFile id={logoId} />",
    "<BowFile id={`${logoId}`} />",
    '<BowFile id={"img-" + theme} />',
    '<BowFile data-id="not-a-file" id={logoId} />',
])
def test_a_computed_id_is_left_to_the_runtime(tag):
    assert bow_file_literal_ids(tag) == set()


def test_ids_have_one_spelling():
    assert normalize_file_id(f"  {FID.upper()} ") == FID
    assert normalize_file_id("Not-A-Uuid ") == "not-a-uuid"


@pytest.mark.parametrize("spelling", [FID.upper(), f" {FID} ", FID.replace("-", ""), "{" + FID + "}"])
def test_a_reference_spelled_differently_from_the_attached_id_is_an_error(spelling):
    # The runtime matches <BowFile id> against files[].id with ===.
    errors = file_reference_errors(f'<BowFile id="{spelling}" />', {"files": [{"id": FID}]})
    assert len(errors) == 1 and FID in errors[0]


def test_an_unattached_reference_is_an_error_and_an_attached_one_is_not():
    code = f'<BowFile id="{FID}" />'
    assert file_reference_errors(code, {"files": [{"id": FID}]}) == []
    errors = file_reference_errors(code, {"files": []})
    assert len(errors) == 1 and FID in errors[0]
    assert file_reference_errors(code, {}) == errors


def test_references_already_broken_can_be_left_alone():
    assert file_reference_errors(f'<BowFile id="{FID}" />', {"files": []}, ignore_ids=[FID]) == []


class _Nested:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        self.db.savepoints += 1

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type:
            self.db.rolled_back_to_savepoint += 1
        return False


class _Db:
    """Just enough of an AsyncSession: a savepoint and one SELECT."""
    def __init__(self, rows=(), fail=False):
        self.rows, self.fail = list(rows), fail
        self.savepoints = self.rolled_back_to_savepoint = 0

    def begin_nested(self):
        return _Nested(self)

    async def execute(self, _stmt):
        if self.fail:
            raise RuntimeError("connection reset")
        rows = self.rows
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


def _file(fid, content_type):
    return SimpleNamespace(id=fid, content_type=content_type, filename="f")


def _lookup(db, ids, monkeypatch, allowed=None):
    async def viewable(_db, *, user, report, organization, file_ids):
        return set(allowed if allowed is not None else file_ids)
    monkeypatch.setattr("app.services.file_access_service.run_viewable_file_ids", viewable)
    return asyncio.run(_artifact_files.attachable_files(db, user=None, report=None, organization=None, file_ids=ids))


def test_only_images_and_pdfs_are_attachable(monkeypatch):
    other = "4f2a1b4c-5d6e-4f70-8a9b-0c1d2e3f4a5c"
    pdf = "5f2a1b4c-5d6e-4f70-8a9b-0c1d2e3f4a5d"
    db = _Db(rows=[_file(FID, "image/png"), _file(other, "text/csv"), _file(pdf, "application/pdf; charset=binary")])
    found = _lookup(db, [FID.upper(), other, pdf], monkeypatch)
    assert set(found) == {FID, pdf}
    assert found[FID]["content_type"] == "image/png"


def test_nothing_requested_means_no_lookup(monkeypatch):
    db = _Db(fail=True)
    assert _lookup(db, ["", None], monkeypatch) == {}
    assert db.savepoints == 0
