"""How much of an MCP result reaches the model inline.

The tool used to hand back three records whatever the caller asked for, which
is enough to infer a schema and not enough to answer a question. These cover
the character budget that replaced it, plus the compaction that keeps the
bigger payload from stacking across a run.
"""
from app.ai.context.builders.observation_context_builder import ObservationContextBuilder
from app.ai.tools.implementations.execute_mcp import (
    _MIN_PREVIEW_ROWS,
    _fit_rows,
    _inline_budget,
    _inline_preview,
    _result_summary,
)


# A generous fixed budget for the fitting tests below.
_BUDGET = 50_000


def _rows(n, width=1):
    return [{"id": i, "name": "x" * width} for i in range(n)]


# -- the budget: no fixed cap, the window decides ---------------------------

class _Settings:
    """An org that still has the old cap stored. It must not apply."""
    def get_config(self, key):
        return type("_C", (), {"value": 50_000})() if key == "mcp_result_inline_chars" else None


def _transcript_budget(window):
    from app.ai.agents.planner.transcript_bridge import transcript_budget_tokens
    return transcript_budget_tokens(type("_S", (), {"context_window_tokens": window})())


def test_a_large_window_takes_a_large_result_whole():
    """A 360k-char export (~203k tokens) fits a 1M-token model's window, so it
    must reach the model whole; 50k of it made the agent page a file."""
    assert _inline_budget(None, 1_000_000) >= 360_000


def test_no_stored_setting_caps_the_result():
    for window in (200_000, 1_000_000):
        assert _inline_budget(_Settings(), window) == _inline_budget(None, window)


def test_budget_grows_with_the_window():
    sizes = [_inline_budget(None, w) for w in (32_000, 128_000, 200_000, 1_000_000)]
    assert sizes == sorted(sizes) and len(set(sizes)) == len(sizes)


def test_unknown_window_uses_the_assumed_window():
    for window in (None, 0, -1, "200k"):
        assert _inline_budget(None, window) == _inline_budget(None, 128_000)


def test_two_protected_dense_results_still_fit_the_transcript():
    """The decay ladder cannot shrink the last two tool results. Dense tabular
    data runs ~1.8 chars per token, so count it that way, not as prose."""
    for window in (32_000, 128_000, 200_000, 1_000_000):
        chars = _inline_budget(None, window)
        dense_tokens = chars / 1.8
        assert 2 * dense_tokens <= _transcript_budget(window) * 1.15, window


# -- fitting ---------------------------------------------------------------

def test_small_result_arrives_whole():
    """The regression that started this: 40 records came back as 3."""
    rows = _rows(40)
    fitted, truncated = _fit_rows(rows, _BUDGET)
    assert fitted == rows
    assert truncated is False


def test_large_result_is_cut_and_says_so():
    rows = _rows(500, width=200)
    fitted, truncated = _fit_rows(rows, 5_000)
    assert truncated is True
    assert 0 < len(fitted) < len(rows)


def test_one_oversized_row_still_yields_one_record():
    """A row wider than the whole budget must not collapse the sample to
    nothing — the shape has to survive for the agent to write a reader."""
    fitted, truncated = _fit_rows(_rows(5, width=50_000), 1_000)
    assert len(fitted) == 1
    assert truncated is True


def test_zero_budget_falls_back_to_a_sample():
    rows = _rows(40)
    fitted, truncated = _fit_rows(rows, 0)
    assert len(fitted) == _MIN_PREVIEW_ROWS
    assert truncated is True

    # Under the floor there is nothing left to cut.
    fitted, truncated = _fit_rows(_rows(2), 0)
    assert len(fitted) == 2
    assert truncated is False


def test_empty_result_is_not_reported_as_truncated():
    assert _fit_rows([], _BUDGET) == ([], False)


# -- the whole payload, not just the detected list --------------------------

def test_envelope_that_fits_goes_inline_whole():
    """The regression this exists for: a wide single-record response whose only
    list is a tiny sidecar (project pegging) was reduced to that sidecar, with
    99% of the budget unspent — and the agent answered from the sidecar."""
    envelope = {
        "ShowResponse": {"ShowResponse": {"DataArea": {"ProductionOrdersSF": {
            "orderNumber": "AYS601037",
            "actualDeliveryDate": "2026-08-20",
            **{f"field{i}": f"value{i}" for i in range(40)},
            "pegging": [{"Project": "X707", "Activity": "PCA80115"}],
        }}}},
    }
    rows = [{"Project": "X707", "Activity": "PCA80115"}]
    preview, shown, truncated, is_full = _inline_preview(
        envelope, rows, "ShowResponse.ShowResponse.DataArea.ProductionOrdersSF.pegging",
        _BUDGET,
    )
    assert preview is envelope
    assert is_full is True
    assert truncated is False
    assert shown == 1


def test_oversized_envelope_falls_back_to_rows():
    envelope = {"data": _rows(10), "noise": "x" * 100_000}
    preview, shown, truncated, is_full = _inline_preview(
        envelope, envelope["data"], "data", 5_000
    )
    assert is_full is False
    assert preview == envelope["data"]
    assert truncated is False  # all 10 narrow rows fit the row budget


def test_bare_list_payload_never_claims_full_payload():
    """path == "": the rows ARE the payload; the flag would be redundant noise."""
    rows = _rows(5)
    preview, shown, truncated, is_full = _inline_preview(rows, rows, "", _BUDGET)
    assert preview == rows
    assert is_full is False
    assert truncated is False


def test_zero_budget_never_inlines_the_envelope():
    envelope = {"data": _rows(40)}
    preview, shown, truncated, is_full = _inline_preview(envelope, envelope["data"], "data", 0)
    assert is_full is False
    assert len(preview) == _MIN_PREVIEW_ROWS
    assert truncated is True


# -- compaction ------------------------------------------------------------

def test_superseded_mcp_preview_is_compacted():
    """Five 50k previews stacking into every later prompt is what this budget
    would otherwise cost, so a superseded one drops to a marker."""
    b = ObservationContextBuilder()
    b.add_tool_observation(
        "execute_mcp", {"tool_name": "get_production_order"},
        {"summary": "Executed", "preview": _rows(400), "file_id": "f1", "row_count": 400},
        loop_index=1,
    )
    b.add_tool_observation("create_data", {}, {"summary": "ok"}, loop_index=2)

    obs = b.tool_observations[0]["observation"]
    assert "preview" not in obs
    assert "400 records" in obs["preview_compacted"]
    # The referenceable bits stay, or a later step can't re-open the result.
    assert obs["file_id"] == "f1"
    assert obs["row_count"] == 400
    assert obs["summary"] == "Executed"


def test_full_payload_preview_compacts_with_char_units():
    """A whole-payload preview is a dict; compaction must not choke on it (or
    report its key count as 'chars')."""
    b = ObservationContextBuilder()
    b.add_tool_observation(
        "execute_mcp", {},
        {"summary": "Executed", "preview": {"env": {"data": _rows(30)}}, "preview_is_full_payload": True},
        loop_index=1,
    )
    b.add_tool_observation("create_data", {}, {"summary": "ok"}, loop_index=2)
    obs = b.tool_observations[0]["observation"]
    assert "preview" not in obs
    shown = int(obs["preview_compacted"].split()[0])
    assert shown > 100  # serialized size, not len(dict) == 1
    assert "chars" in obs["preview_compacted"]


def test_text_preview_compacts_with_char_units():
    b = ObservationContextBuilder()
    b.add_tool_observation(
        "execute_mcp", {}, {"summary": "Executed", "preview": "y" * 900}, loop_index=1,
    )
    b.add_tool_observation("execute_mcp", {}, {"summary": "Executed"}, loop_index=2)
    assert "900 chars" in b.tool_observations[0]["observation"]["preview_compacted"]


def test_same_batch_observations_keep_full_previews():
    """A parallel multi-tool batch is recorded one entry at a time; the planner
    must see the whole finished batch, not just whichever landed last."""
    b = ObservationContextBuilder()
    b.add_tool_observation("execute_mcp", {}, {"summary": "a", "preview": _rows(50)}, loop_index=7)
    b.add_tool_observation("execute_mcp", {}, {"summary": "b", "preview": _rows(50)}, loop_index=7)
    assert len(b.tool_observations[0]["observation"]["preview"]) == 50


# -- what the summary tells the agent to do next ---------------------------

def _tabular(**over):
    out = {
        "file_id": "f1", "file_name": "get_production_order.json",
        "row_count": 400, "media": "json", "tabular_path": "data",
    }
    out.update(over)
    return _result_summary("get_production_order", "tabular", out)


def test_full_payload_result_says_answer_directly():
    """The whole point of the bigger budget: no second call to re-read data
    the agent can already see."""
    s = _tabular(row_count=40, preview_truncated=False, preview_is_full_payload=True)
    assert "COMPLETE response is shown above" in s
    assert "answer directly" in s


def test_bare_list_result_says_answer_directly():
    s = _tabular(row_count=40, preview_truncated=False, tabular_path="")
    assert "All of them are shown above" in s
    assert "answer directly" in s


def test_partial_envelope_result_warns_about_the_rest():
    """Untruncated rows from an envelope too big to inline whole: the list is
    complete, the response is not — the old 'all shown, answer directly' here
    is exactly how a tiny sidecar list became the whole answer."""
    s = _tabular(row_count=1, preview_truncated=False)
    assert "answer directly" not in s
    assert "MORE outside 'data'" in s
    assert "read_file(file_id='f1')" in s


def test_truncated_result_routes_reading_to_read_file():
    """It used to name create_data for this — generating pandas to look at
    records, when read_file returns them directly."""
    s = _tabular(preview_truncated=True, preview_row_count=72)
    assert "read_file(file_id='f1'" in s
    assert "72" in s
    assert "do NOT answer from those alone" in s
    # Byte-window caveat must travel with the advice, not be discovered.
    assert "BYTE offsets" in s


def test_truncated_result_prefers_the_server_cursor_when_there_is_one():
    """Paging the source is the only route that returns records this call
    never fetched; reading our copy cannot."""
    s = _tabular(
        preview_truncated=True, preview_row_count=72,
        result_metadata={"next_cursor": "abc123"},
    )
    idx_cursor = s.index("call 'get_production_order' again")
    idx_read = s.index("read_file(file_id='f1'")
    assert idx_cursor < idx_read, "cursor advice must lead"
    assert "abc123" in s


def test_analysis_still_routes_to_create_data():
    for s in (_tabular(preview_truncated=False), _tabular(preview_truncated=True, preview_row_count=5)):
        assert "create_data(source_file_ids=['f1'])" in s
        assert "write_csv(source_file_ids=['f1'])" in s


def test_oversized_unparsed_result_offers_both_reading_and_analysis():
    s = _result_summary("bulk_export", "json", {
        "file_id": "f2", "file_name": "bulk_export.json",
        "media": "json", "parse_skipped": True, "size_chars": 8_000_000,
    })
    assert "read_file(file_id='f2'" in s
    assert "create_data(source_file_ids=['f2'])" in s
    assert "8,000,000 characters" in s


def test_failed_materialization_never_claims_a_file():
    s = _result_summary("x", "tabular", {"materialization_error": "disk full", "row_count": 3})
    assert "could NOT be saved" in s
    assert "do not assume a file exists" in s
