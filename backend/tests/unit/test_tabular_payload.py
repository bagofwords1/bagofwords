"""A table wrapped in an API envelope must be recognized as tabular.

Regression: a contacts search returned {"type": "list", "data": [...150...],
"pages": {...}}. Detection only matched a top-level array, so the 150 rows were
saved as an opaque .json blob with no preview; the generated code reached for
`pd.read_excel` on that path, swallowed the resulting error and shipped a
0-row DataFrame that looked like a successful export.
"""
import json
import os

import pytest

from app.ai.agents.coder.coder import _excel_files_mapping, _file_access_rules
from app.data_sources.clients.mcp_client import McpClient
from app.services.file_preview import generate_file_preview, render_file_index_line
from app.utils.tabular_payload import (
    detect_content_type,
    envelope_metadata,
    extract_tabular_rows,
    find_table,
)


def _contacts(n=150):
    return [
        {"id": f"c{i}", "name": f"Contact {i}", "email": f"c{i}@example.com", "role": "user"}
        for i in range(n)
    ]


def _intercom_payload(n=150):
    """The exact shape that produced the 0-row export."""
    return {
        "type": "list",
        "data": _contacts(n),
        "total_count": n,
        "pages": {"type": "pages", "next": "https://api.intercom.io/contacts?page=2", "page": 1},
    }


# --- detection ---------------------------------------------------------------

def test_envelope_wrapped_table_is_tabular():
    payload = _intercom_payload()
    assert detect_content_type(payload) == "tabular"
    rows, path = find_table(payload)
    assert path == "data"
    assert len(rows) == 150
    assert rows[0]["email"] == "c0@example.com"


def test_bare_top_level_array_still_tabular():
    assert detect_content_type(_contacts(3)) == "tabular"
    assert find_table(_contacts(3))[1] == ""


def test_text_and_plain_json_unchanged():
    assert detect_content_type("some search result text") == "text"
    assert detect_content_type({"status": "ok", "count": 3}) == "json"
    assert detect_content_type({"user": {"name": "x", "org": {"id": 1}}}) == "json"
    assert detect_content_type([]) == "json"
    assert extract_tabular_rows({"status": "ok"}) is None


def test_alternative_envelope_keys_and_nesting():
    for key in ("results", "items", "records", "rows", "entries"):
        assert find_table({key: _contacts(2), "next": None})[1] == key
    # {"result": {"data": [...]}} — two layers of envelope.
    assert find_table({"result": {"data": _contacts(2)}})[1] == "result.data"
    # An unconventional key is still unambiguous when it's the only candidate.
    assert find_table({"contacts_found": _contacts(2), "took_ms": 12})[1] == "contacts_found"


def test_ambiguous_payload_is_left_as_json():
    """Two candidate tables — picking one would be a guess, so pick neither."""
    payload = {"contacts": _contacts(2), "companies": _contacts(2)}
    assert detect_content_type(payload) == "json"


def test_envelope_metadata_preserves_pagination():
    payload = _intercom_payload()
    metadata = envelope_metadata(payload, "data")
    assert metadata["total_count"] == 150
    assert metadata["pages"]["next"].endswith("page=2")
    assert "data" not in metadata


def test_mcp_client_detects_envelope():
    client = McpClient.__new__(McpClient)  # no transport needed for detection
    assert client._detect_content_type(_intercom_payload()) == "tabular"
    assert client._detect_content_type("plain text") == "text"


# --- previews ----------------------------------------------------------------

def test_json_file_preview_reports_structure(tmp_path):
    path = tmp_path / "search_contacts.json"
    path.write_text(json.dumps(_intercom_payload()))

    class _File:
        filename = "search_contacts.json"
        content_type = "application/json"

    f = _File()
    f.path = str(path)
    preview = generate_file_preview(f)

    structure = preview.get("json_structure")
    assert structure, "a JSON file must carry a structural preview, not just a 500-char head"
    assert structure["table_path"] == "data"
    assert structure["row_count"] == 150
    assert "email" in structure["columns"]
    assert "data" in structure["keys"]

    line = render_file_index_line(preview, str(path), filename=f.filename)
    assert "150 records at 'data'" in line
    assert "email" in line


def test_index_line_names_type_when_preview_missing():
    class _File:
        id = "f1"
        filename = "search_contacts.json"
        path = "uploads/files/search_contacts.json"
        content_type = "application/json"
        preview = None

    line = _excel_files_mapping([_File()])
    assert "content_type: application/json" in line


# --- materialization ---------------------------------------------------------

class _FakeNested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeDb:
    """Just enough AsyncSession for the materialize helpers."""
    def __init__(self):
        self.added = []

    def begin_nested(self):
        return _FakeNested()

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def execute(self, *a, **kw):
        pass


class _User:
    id = "user-123"


class _Org:
    id = "org-456"


@pytest.mark.asyncio
async def test_materialized_file_is_owned_by_the_acting_user():
    """files.user_id is NOT NULL, and the agent loop supplies the user under
    'user' — reading only 'current_user' made every materialization inside an
    agent run die on the insert."""
    from app.ai.tools.implementations.execute_mcp import ExecuteMCPTool

    tool = ExecuteMCPTool.__new__(ExecuteMCPTool)
    ctx = {"db": _FakeDb(), "report": None, "organization": _Org(), "user": _User()}

    csv_file = await tool._materialize_to_csv(_contacts(3), "search_contacts", ctx)
    assert csv_file.user_id == "user-123"
    assert csv_file.organization_id == "org-456"

    json_file = await tool._materialize_to_json(_intercom_payload(2), "search_contacts", ctx)
    assert json_file.user_id == "user-123"
    # And the JSON file gets a preview, so the coder isn't reading a blind filename.
    assert json_file.preview and json_file.preview.get("json_structure")

    for f in (csv_file, json_file):
        if os.path.exists(f.path):
            os.remove(f.path)


# --- codegen instructions ----------------------------------------------------

def test_file_access_rules_cover_non_excel_and_forbid_swallowing():
    rules = _file_access_rules()
    assert "pd.read_csv" in rules
    assert "pd.read_json" in rules
    assert "json_normalize" in rules
    # `open` is sandbox-forbidden, so JSON has to be read through pandas.
    assert "open()` is sandbox-forbidden" in rules
    assert "NEVER call `pd.read_excel` on a `.json`" in rules
    assert "try/except" in rules
