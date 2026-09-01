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


def test_deep_bod_envelope_is_found():
    """ERP-style BOD nesting — {"ShowResponse": {"ShowResponse": {"DataArea":
    {...}}}} — put the rows at level 5; the old 3-level cutoff classified the
    whole response as opaque JSON."""
    payload = {"ShowResponse": {"ShowResponse": {"DataArea": {
        "ProductionOrders": {"order": _contacts(4), "recordCount": 4},
    }}}}
    rows, path = find_table(payload)
    assert path == "ShowResponse.ShowResponse.DataArea.ProductionOrders.order"
    assert len(rows) == 4


def test_multi_branch_envelope_is_found():
    """The strict descent recurses only through a SOLE dict child, so an
    ApplicationArea sitting beside the DataArea used to hide the table."""
    payload = {"ShowResponse": {
        "ApplicationArea": {"Sender": {"LogicalID": "lnapp"}, "CreationDateTime": "2026-09-01"},
        "DataArea": {"Show": {"records": _contacts(3)}},
    }}
    rows, path = find_table(payload)
    assert path == "ShowResponse.DataArea.Show.records"
    assert len(rows) == 3


def test_ambiguous_payload_is_left_as_json():
    """Two candidate tables of the SAME size — picking one would be a coin
    flip, so pick neither. (A candidate that wins on length is taken; see
    test_rows_win_over_a_short_sidecar_list.)"""
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

    json_file = await tool._materialize_to_json(_intercom_payload(2), "search_contacts", ctx)
    assert json_file.user_id == "user-123"
    assert json_file.organization_id == "org-456"
    # And the JSON file gets a preview, so the coder isn't reading a blind filename.
    assert json_file.preview and json_file.preview.get("json_structure")

    text_file = await tool._materialize_to_text("shift notes", "get_notes", ctx)
    assert text_file.user_id == "user-123"

    for f in (json_file, text_file):
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


# --- multi-candidate payloads ------------------------------------------------
#
# The shape that broke a real run: rows under a domain-specific key with a short
# sidecar list beside them. "Two candidates → give up" classified 430 work
# orders as an opaque blob, and every downstream tool was left with nothing to
# read.

def _work_orders(n=430):
    return [{"orderNumber": f"WO{900000 + i}", "planner": f"P-10{i % 5}",
             "quantityOrdered": i} for i in range(n)]


def _mfg_payload(n=430):
    return {
        "WorkOrdersMFG": _work_orders(n),
        "validationWarnings": [
            {"code": "W-1180", "severity": "info", "message": "no confirmed date"},
            {"code": "W-2245", "severity": "warning", "message": "routing empty"},
        ],
        "recordCount": n,
    }


def test_rows_win_over_a_short_sidecar_list():
    payload = _mfg_payload()
    assert detect_content_type(payload) == "tabular"
    rows, path = find_table(payload)
    assert path == "WorkOrdersMFG"
    assert len(rows) == 430


def test_table_candidates_reports_the_runners_up():
    """The pick must stay visible — a wrong one has to be correctable."""
    from app.utils.tabular_payload import table_candidates

    assert table_candidates(_mfg_payload()) == [
        ("WorkOrdersMFG", 430),
        ("validationWarnings", 2),
    ]


# --- reshaped tables ---------------------------------------------------------

def test_mapping_keyed_by_id_is_a_table():
    rows, _ = find_table({"c1": {"name": "a", "q": 1}, "c2": {"name": "b", "q": 2}})
    assert rows == [{"key": "c1", "name": "a", "q": 1}, {"key": "c2", "name": "b", "q": 2}]


def test_key_column_does_not_clobber_an_existing_key_field():
    rows, _ = find_table({"r1": {"key": "own", "v": 1}, "r2": {"key": "own2", "v": 2}})
    assert rows[0]["_key"] == "r1" and rows[0]["key"] == "own"


def test_column_oriented_payload_is_a_table():
    rows, _ = find_table({"name": ["a", "b"], "qty": [1, 2]})
    assert rows == [{"name": "a", "qty": 1}, {"name": "b", "qty": 2}]


def test_ordinary_json_object_is_not_reshaped():
    assert detect_content_type({"status": "ok", "count": 3}) == "json"
    assert detect_content_type({"user": {"id": 1}}) == "json"


# --- text that is really a table ---------------------------------------------

def test_text_payloads_are_sniffed_for_tables():
    from app.utils.tabular_payload import parse_text_payload

    kind, rows = parse_text_payload("name,qty\na,1\nb,2")
    assert kind == "csv" and len(rows) == 2 and rows[0]["name"] == "a"

    kind, rows = parse_text_payload('{"a": 1}\n{"a": 2}')
    assert kind == "ndjson" and len(rows) == 2

    kind, rows = parse_text_payload('{"data": [{"a": 1}]}')
    assert kind == "json" and len(rows) == 1


def test_prose_is_not_mistaken_for_a_table():
    """csv.Sniffer finds a delimiter in any English text, and TEMPLATED prose
    even yields a consistent column count — so shape alone is not evidence.
    These are the shapes that got misfiled as CSV before the header check."""
    from app.utils.tabular_payload import parse_text_payload

    assert parse_text_payload("The build failed.\nPlease retry later.") == ("text", None)
    assert parse_text_payload("") == ("text", None)
    # Repeated handover notes: same comma count on every line, so every row is
    # the same width. The give-away is the header — a sentence, not a label.
    notes = "\n\n".join(
        f"Shift handover, line {ln}: lead reported the changeover ran {m} minutes "
        "over plan. Torque calibration was repeated."
        for ln, m in (("A", 12), ("B", 4), ("C", 27))
    )
    assert parse_text_payload(notes) == ("text", None)
    # One data row is not enough evidence to call something a table.
    assert parse_text_payload("name,qty\na,1") == ("text", None)


def test_real_delimited_text_is_still_recognized():
    from app.utils.tabular_payload import parse_text_payload

    for blob in ("name,qty,region\na,1,eu\nb,2,us", "name\tqty\na\t1\nb\t2", "id;total\n1;5\n2;6"):
        kind, rows = parse_text_payload(blob)
        assert kind == "csv" and len(rows) == 2, blob


# --- the artifact descriptor -------------------------------------------------

def test_record_shape_describes_the_columns():
    from app.ai.tools.implementations.execute_mcp import _record_shape

    shape = _record_shape([
        {"id": "a", "qty": 3, "price": 1.5, "ok": True, "meta": {"x": 1}, "note": None},
        {"id": "b", "qty": 4, "price": 2.0, "ok": False, "meta": {}, "note": "hi"},
    ])
    assert shape["row_count"] == 2
    assert shape["columns"]["id"] == "str"
    assert shape["columns"]["qty"] == "int"
    assert shape["columns"]["price"] == "float"
    assert shape["columns"]["ok"] == "bool"
    assert shape["columns"]["meta"] == "nested"
    # A column that is null in the first row but populated later reports the
    # real type — otherwise the consumer writes its reader against "null".
    assert shape["columns"]["note"] == "str"
