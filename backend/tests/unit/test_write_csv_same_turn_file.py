"""A file written by write_csv is visible to the next tool call in the same
turn: it joins runtime_ctx["excel_files"] and the report's loaded files, the
two collections resolve_source_files reads (app/ai/tools/implementations/
_source_files.py). Seen live: a sub-agent wrote a CSV and the next
inspect_data on its file_id was refused."""
from types import SimpleNamespace

from app.ai.tools.implementations._source_files import resolve_source_files
from app.ai.tools.implementations.execute_mcp import _register_same_turn


def test_registered_file_resolves_by_id_in_the_same_turn():
    new_file = SimpleNamespace(id="f-new", filename="out.csv", path="/tmp/out.csv", content_type="text/csv")
    runtime_ctx = {"excel_files": [], "report": SimpleNamespace(files=[])}
    _, _, missing_before = resolve_source_files(runtime_ctx, ["f-new"])
    assert missing_before == ["f-new"]

    _register_same_turn(new_file, runtime_ctx, runtime_ctx["report"])
    scoped, _, missing = resolve_source_files(runtime_ctx, ["f-new"])
    assert missing == [] and [f.id for f in scoped] == ["f-new"]
    # idempotent: registering twice does not duplicate
    _register_same_turn(new_file, runtime_ctx, runtime_ctx["report"])
    assert [f.id for f in runtime_ctx["excel_files"]] == ["f-new"]
