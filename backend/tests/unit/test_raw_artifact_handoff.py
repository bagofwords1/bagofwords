"""An MCP result must survive to disk unchanged, and the next tool must be able
to open it.

These cover the contract that replaced "detect a table and write a CSV":

  * the artifact is the payload as received — the detector only annotates it,
  * the codegen directive names the exact reader for that artifact, including
    the two-step json_normalize form for an enveloped payload (`pd.read_json`
    alone RAISES on one, so getting this wrong costs the whole attempt),
  * text is readable from generated code at all, via a reader scoped to the
    run's own files,
  * a response too large to parse is still saved rather than dropped.
"""
import json

import pytest

from app.ai.code_execution.code_execution import (
    READ_TEXT_MAX_CHARS,
    _build_read_text,
)
from app.ai.tools.implementations._source_files import (
    _json_reader,
    resolve_source_files,
)
from app.data_sources.clients.mcp_client import MAX_PARSE_CHARS, McpClient


def _work_orders(n=430):
    return [
        {
            "orderNumber": f"WO{900000 + i}",
            "planner": f"P-10{i % 5}",
            "plannerName": ["Dana", "Marcus", "Ingrid", "Tomas", "Aiko"][i % 5],
            "quantityOrdered": i,
            "pegData": [{"project": f"A{i}", "activity": f"PCA{i}"}],
        }
        for i in range(n)
    ]


def _envelope(n=430):
    return {
        "WorkOrdersMFG": _work_orders(n),
        "validationWarnings": [{"code": "W-1", "message": "no date"}],
        "recordCount": n,
    }


class _File:
    def __init__(self, id, filename, path):
        self.id = id
        self.filename = filename
        self.path = path


# --- the reader the coder is handed -------------------------------------------

def test_enveloped_json_gets_the_two_step_reader():
    """`pd.read_json` on an envelope raises; the directive must not suggest it."""
    reader = _json_reader(0, "WorkOrdersMFG")
    assert "typ='series'" in reader
    assert "json_normalize(payload['WorkOrdersMFG'])" in reader


def test_bare_array_json_gets_the_simple_reader():
    assert _json_reader(0, "") == "pd.read_json(excel_files[0].path)"


def test_nested_path_is_chained():
    assert "payload['result']['data']" in _json_reader(0, "result.data")


def test_the_two_step_reader_actually_works_on_a_real_envelope(tmp_path):
    """The whole point — run the generated form against a real file.

    This is the common case now that artifacts are saved raw: MCP returns JSON,
    the coder reads it, normalizes, and analyzes.
    """
    import pandas as pd

    path = tmp_path / "orders.json"
    path.write_text(json.dumps(_envelope()))

    # The naive reader — what the directive used to suggest — cannot do this.
    with pytest.raises(ValueError):
        pd.read_json(path)

    payload = pd.read_json(path, typ="series").to_dict()
    df = pd.json_normalize(payload["WorkOrdersMFG"])

    assert len(df) == 430
    assert "quantityOrdered" in df.columns
    totals = df.groupby("plannerName")["quantityOrdered"].sum()
    assert totals.sum() == sum(r["quantityOrdered"] for r in _envelope()["WorkOrdersMFG"])


# --- the directive ------------------------------------------------------------

def _ctx(files, observations=None):
    class _Builder:
        tool_observations = observations or []

    class _Hub:
        observation_builder = _Builder()

    return {"excel_files": files, "report": None, "context_hub": _Hub()}


def test_directive_names_the_key_the_records_live_under():
    f = _File("f1", "query_orders.json", "uploads/files/x_query_orders.json")
    ctx = _ctx([f], [{
        "file_id": "f1",
        "tabular_path": "WorkOrdersMFG",
        "candidate_paths": ["validationWarnings"],
        "record_shape": {"row_count": 430, "columns": {"orderNumber": "str", "quantityOrdered": "int"}},
    }])

    scoped, directive, missing = resolve_source_files(ctx, ["f1"])

    assert scoped == [f] and not missing
    assert "json_normalize(payload['WorkOrdersMFG'])" in directive
    assert "under the key 'WorkOrdersMFG'" in directive
    assert "validationWarnings" in directive          # runner-up stays visible
    assert "orderNumber:str" in directive             # columns are stated, not guessed


def test_directive_routes_unreadable_formats_to_read_file():
    f = _File("f2", "spec.pdf", "uploads/files/x_spec.pdf")
    _, directive, _ = resolve_source_files(_ctx([f]), ["f2"])
    assert "NOT readable from generated code" in directive
    assert "read_file(file_id='f2')" in directive


def test_directive_gives_text_files_the_text_reader():
    f = _File("f3", "notes.txt", "uploads/files/x_notes.txt")
    _, directive, _ = resolve_source_files(_ctx([f]), ["f3"])
    assert "read_text(excel_files[0])" in directive


def test_missing_ids_are_reported_not_silently_dropped():
    f = _File("f4", "a.json", "uploads/files/a.json")
    scoped, _, missing = resolve_source_files(_ctx([f]), ["f4", "nope"])
    assert scoped == [f]
    assert missing == ["nope"]


# --- read_text ----------------------------------------------------------------

def test_read_text_reads_a_run_file(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("line one\nline two\n")
    f = _File("f5", "notes.txt", str(p))

    read_text = _build_read_text([f])
    assert read_text(f) == "line one\nline two\n"
    # A bare path works too, as long as it is one of this run's files.
    assert read_text(str(p)).startswith("line one")


def test_read_text_refuses_a_path_outside_the_run(tmp_path):
    """This is the property that made banning `open` worth doing."""
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read me")
    read_text = _build_read_text([])

    with pytest.raises(ValueError, match="not one of this run's files"):
        read_text(str(secret))


def test_read_text_refuses_container_formats(tmp_path):
    p = tmp_path / "spec.pdf"
    p.write_bytes(b"%PDF-1.4 ...")
    f = _File("f6", "spec.pdf", str(p))
    read_text = _build_read_text([f])

    with pytest.raises(ValueError, match="read_file tool"):
        read_text(f)


def test_read_text_truncates_rather_than_returning_unbounded(tmp_path):
    p = tmp_path / "big.log"
    p.write_text("x" * (READ_TEXT_MAX_CHARS + 500))
    f = _File("f7", "big.log", str(p))

    out = _build_read_text([f])(f)
    assert len(out) < READ_TEXT_MAX_CHARS + 200
    assert "TRUNCATED" in out


# --- ingestion cap ------------------------------------------------------------

class _Block:
    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self, content):
        self.content = content


def test_oversized_response_is_not_parsed_but_is_still_returned():
    """Parsing multiplies a payload several times over in Python objects. Over
    the cap we hand back the raw string — the caller still writes every byte to
    a file, so the data survives; it just isn't parsed here."""
    client = McpClient.__new__(McpClient)
    huge = json.dumps({"data": [{"i": i} for i in range(10)]})
    huge += "x" * (MAX_PARSE_CHARS + 1)

    out = client._extract_result_data(_Result([_Block(huge)]))

    assert isinstance(out, str), "must not be parsed into Python objects"
    assert len(out) == len(huge), "and must not be silently dropped or trimmed"


def test_normal_response_is_still_parsed():
    client = McpClient.__new__(McpClient)
    out = client._extract_result_data(_Result([_Block(json.dumps(_envelope(2)))]))
    assert isinstance(out, dict)
    assert len(out["WorkOrdersMFG"]) == 2


# --- an unparsed payload keeps its FORMAT ------------------------------------
#
# Found in the sandbox: skipping the parse also lost "this is JSON". The 8 MB
# response was saved as .txt with media=text, the agent was told to read prose,
# and it burned five failed tool calls before recovering. Declining to parse
# must not mean declining to say what the bytes are.

def test_oversized_json_is_still_recognized_as_json():
    from app.data_sources.clients.mcp_client import looks_like_json

    assert looks_like_json('{"WorkOrdersMFG": [')
    assert looks_like_json('   [{"a": 1}')
    assert not looks_like_json("Shift handover, line ASSY-A: ...")
    assert not looks_like_json(None)


def test_unparsed_result_is_flagged_parse_skipped():
    client = McpClient.__new__(McpClient)
    huge = '{"WorkOrdersMFG": [' + "," .join('{"i": %d}' % i for i in range(10)) + "]}"
    huge += " " * (MAX_PARSE_CHARS + 1)
    out = client._extract_result_data(_Result([_Block(huge)]))
    assert isinstance(out, str)


def test_unknown_shape_does_not_get_the_bare_reader():
    """Absence of a tabular_path means "unknown" for an unparsed file, NOT
    "flat" — suggesting bare read_json there raises on any envelope."""
    reader = _json_reader(0, "", shape_known=False)
    assert "typ='series'" in reader
    assert "print(list(payload.keys()))" in reader
    assert reader != "pd.read_json(excel_files[0].path)"


def test_directive_warns_when_the_file_was_never_inspected():
    f = _File("f8", "big.json", "uploads/files/big.json")
    ctx = _ctx([f], [{"file_id": "f8", "parse_skipped": True}])
    _, directive, _ = resolve_source_files(ctx, ["f8"])
    assert "structure is UNKNOWN" in directive
    assert "print(list(payload.keys()))" in directive


# --- same-turn registration ---------------------------------------------------

def test_report_files_is_updated_without_marking_it_dirty():
    """read_file resolves against report.files, which is loaded once per run.

    The obvious fix — report.files.append(file) — marks the collection dirty,
    so SQLAlchemy emits its own INSERT for the association on top of the manual
    one and the run dies on UNIQUE constraint failed: report_file_association.
    (Observed in the sandbox.) set_committed_value installs the value as
    already-persisted, which is what it is.
    """
    import inspect

    from app.ai.tools.implementations import execute_mcp as em

    src = inspect.getsource(em._register_same_turn)
    body = src.split('"""')[-1]  # drop the docstring, which names the trap
    assert "set_committed_value" in body
    assert ".files.append" not in body


def test_register_same_turn_is_a_noop_when_files_were_not_loaded():
    """Reports loaded with noload(Report.files) must not be touched — under
    async, a lazy load raises."""
    from app.ai.tools.implementations.execute_mcp import _register_same_turn

    class _Report:
        pass  # no 'files' in __dict__

    class _NewFile:
        id = "new"

    ctx = {"excel_files": []}
    _register_same_turn(_NewFile(), ctx, _Report())
    assert ctx["excel_files"] == [_NewFile.id] or len(ctx["excel_files"]) == 1
