"""The data of a published step that waits for a viewer's input.

A step whose query needs a required input nobody supplied is not run (at
Publish, at a refresh, in a reader's dashboard run). What it holds instead is
a "waiting frame": no rows, the marker `info.needs_input`, and the copy's
columns only on the shared copy. A reader's waiting row that a broken source
blocks is stored as that source's error instead, with its fixed reason.
"""
from app.services.access_errors import CREDENTIALS_REQUIRED_REASON, NO_ACCESS_REASON
from app.services import dashboard_runs as runs

COPY = {"rows": [{"לקוח": "טיוטה", "הכנסות": 1}], "columns": [{"field": "לקוח"}, {"field": "הכנסות"}],
        "info": {"total_rows": 1, "total_columns": 2}}


def test_a_waiting_frame_keeps_the_copys_columns_and_none_of_its_rows():
    frame = runs.needs_input_frame(COPY, ["customer_id"])
    assert frame["rows"] == []
    assert frame["columns"] == [{"field": "לקוח"}, {"field": "הכנסות"}]
    assert frame["info"] == {"total_rows": 0, "total_columns": 2, "needs_input": ["customer_id"]}
    assert COPY["rows"], "the copy it was built from is left alone"


def test_a_readers_waiting_frame_has_no_columns():
    frame = runs.needs_input_frame(None, ["customer_id", "region"])
    assert frame == {"rows": [], "columns": [], "info": {"total_rows": 0, "total_columns": 0,
                                                         "needs_input": ["customer_id", "region"]}}


def test_needs_input_names_reads_only_a_waiting_frame():
    assert runs.needs_input_names(runs.needs_input_frame(COPY, ["customer_id"])) == ["customer_id"]
    assert runs.needs_input_names(runs.needs_input_frame(None, ["a", "b"])) == ["a", "b"]
    for other in (None, {}, COPY, {"info": {"needs_input": []}}, {"info": "x"}, {"info": {"needs_input": "a"}},
                  "not a dict"):
        assert runs.needs_input_names(other) == [], other


def test_nothing_blocks_a_wait_without_source_errors():
    assert runs.input_wait_blocked_reason([]) is None


def test_a_source_the_reader_must_connect_blocks_first():
    errors = [{"code": "no_access", "error": "denied"}, {"code": "credentials_required", "error": "Connect required"}]
    assert runs.input_wait_blocked_reason(errors) == CREDENTIALS_REQUIRED_REASON


def test_a_source_the_reader_may_not_use_blocks_with_no_access():
    assert runs.input_wait_blocked_reason([{"code": "no_access", "error": "denied"}]) == NO_ACCESS_REASON


def test_any_other_source_failure_blocks_with_its_own_text():
    assert runs.input_wait_blocked_reason([{"code": "connection_failed", "error": "timeout talking to the warehouse"}]) \
        == "timeout talking to the warehouse"
    assert runs.input_wait_blocked_reason([{"code": "connection_failed", "error": ""}]) == "connection_failed"
    assert runs.input_wait_blocked_reason([{"code": "connection_failed"}]) == "connection_failed"


class _Step:
    def __init__(self, sid):
        self.id = sid


def test_a_step_waits_for_the_inputs_of_every_waiting_step_it_loads():
    steps = {"base": _Step("b"), "other": _Step("o"), "plain": _Step("p")}
    data = {"b": runs.needs_input_frame(COPY, ["customer_id"]), "o": runs.needs_input_frame(None, ["region", "customer_id"]),
            "p": COPY}
    code = ("def generate_df(ds_clients, excel_files, load_step):\n"
            "    return load_step('base').merge(load_step('other')).merge(load_step('plain')).merge(load_step('gone'))\n")
    names = runs.loaded_input_wait(code, steps.get, lambda dep: data.get(dep.id))
    assert sorted(names) == ["customer_id", "region"], "each once, whichever loaded step it came from"
    plain = "def generate_df(ds_clients, excel_files, load_step):\n    return load_step('plain')\n"
    assert runs.loaded_input_wait(plain, steps.get, lambda dep: data.get(dep.id)) == []
    assert runs.loaded_input_wait(None, steps.get, lambda dep: data.get(dep.id)) == []
