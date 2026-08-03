"""How much of an MCP result reaches the model inline.

The tool used to hand back three records whatever the caller asked for, which
is enough to infer a schema and not enough to answer a question. These cover
the character budget that replaced it, plus the compaction that keeps the
bigger payload from stacking across a run.
"""
from app.ai.context.builders.observation_context_builder import ObservationContextBuilder
from app.ai.tools.implementations.execute_mcp import (
    _DEFAULT_INLINE_CHARS,
    _MAX_INLINE_CHARS,
    _MIN_PREVIEW_ROWS,
    _fit_rows,
    _inline_budget,
)


class _Cfg:
    def __init__(self, value):
        self.value = value


class _Settings:
    """Stand-in for OrganizationSettings — only get_config is read."""
    def __init__(self, value):
        self._value = value

    def get_config(self, key):
        return _Cfg(self._value) if key == "mcp_result_inline_chars" else None


def _rows(n, width=1):
    return [{"id": i, "name": "x" * width} for i in range(n)]


# -- the budget ------------------------------------------------------------

def test_budget_defaults_when_unset_or_unreadable():
    assert _inline_budget(None) == _DEFAULT_INLINE_CHARS
    assert _inline_budget(_Settings("not-a-number")) == _DEFAULT_INLINE_CHARS


def test_budget_is_clamped_both_ways():
    # A negative stored value must not become an unbounded slice.
    assert _inline_budget(_Settings(-1)) == 0
    assert _inline_budget(_Settings(10_000_000)) == _MAX_INLINE_CHARS
    assert _inline_budget(_Settings(1234)) == 1234


# -- fitting ---------------------------------------------------------------

def test_small_result_arrives_whole():
    """The regression that started this: 40 records came back as 3."""
    rows = _rows(40)
    fitted, truncated = _fit_rows(rows, _DEFAULT_INLINE_CHARS)
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
    assert _fit_rows([], _DEFAULT_INLINE_CHARS) == ([], False)


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
