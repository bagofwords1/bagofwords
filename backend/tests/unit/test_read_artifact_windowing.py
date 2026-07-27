"""Unit tests for read_artifact long-artifact windowing: range reads, grep
reads, outline building, and observation compaction of windowed payloads."""

import pytest

from app.ai.tools.implementations.read_artifact import (
    FULL_READ_MAX_CHARS,
    build_outline,
    grep_code,
    slice_range,
)
from app.ai.context.builders.observation_context_builder import ObservationContextBuilder


SAMPLE_JSX = "\n".join([
    "// ─── KPI section ───────────────────────",
    "function App() {",
    "  const data = useArtifactData();",
    "  const { filters, setFilter, filterRows } = useFilters();",
    "  const revenue = 1;",
    "  return (",
    '    <SectionCard title="Revenue by Region">',
    "      <EChart option={chartOption} height={300} />",
    "    </SectionCard>",
    "  );",
    "}",
    "const buildChart = (rows) => {",
    "  return { xAxis: {}, series: [] };",
    "};",
    "ReactDOM.createRoot(document.getElementById('root')).render(<App />);",
])


# ─── slice_range ─────────────────────────────────────────────────────────────

def test_slice_range_basic():
    text, start, end, total, truncated = slice_range(SAMPLE_JSX, 2, 3)
    assert start == 2 and end == 4
    assert total == 15
    assert truncated is True
    assert text.split("\n") == [
        "function App() {",
        "  const data = useArtifactData();",
        "  const { filters, setFilter, filterRows } = useFilters();",
    ]


def test_slice_range_verbatim_lines():
    # The slice must be quotable in SEARCH blocks: exact substring of the code.
    text, *_ = slice_range(SAMPLE_JSX, 6, 4)
    assert text in SAMPLE_JSX


def test_slice_range_to_end_not_truncated():
    text, start, end, total, truncated = slice_range(SAMPLE_JSX, 14, 100)
    assert start == 14 and end == 15 and truncated is False
    assert text.endswith("render(<App />);")


def test_slice_range_offset_beyond_end():
    text, start, end, total, truncated = slice_range(SAMPLE_JSX, 999, 10)
    assert text == "" and total == 15 and truncated is False
    assert start == 999 and end == 998  # empty range signalled by end < start


def test_slice_range_default_limit():
    long_code = "\n".join(f"line {i}" for i in range(1, 1001))
    text, start, end, total, truncated = slice_range(long_code, 1, None)
    assert start == 1 and end == 400 and total == 1000 and truncated is True
    assert len(text.split("\n")) == 400


# ─── grep_code ───────────────────────────────────────────────────────────────

def test_grep_basic_match_with_context():
    matches, total, truncated = grep_code(SAMPLE_JSX, r"SectionCard title", before=1, after=1)
    assert total == 1 and truncated is False
    m = matches[0]
    assert m["line_no"] == 7
    assert m["line"] == '    <SectionCard title="Revenue by Region">'
    assert m["before"] == ["  return ("]
    assert m["after"] == ["      <EChart option={chartOption} height={300} />"]
    assert m["context_start_line"] == 6


def test_grep_verbatim_context_lines():
    # before/line/after joined must be an exact substring of the code (quotable).
    matches, _, _ = grep_code(SAMPLE_JSX, r"EChart", before=2, after=2)
    m = matches[0]
    joined = "\n".join([*m["before"], m["line"], *m["after"]])
    assert joined in SAMPLE_JSX


def test_grep_ignore_case():
    matches, total, _ = grep_code(SAMPLE_JSX, r"sectioncard", ignore_case=True)
    assert total == 2  # opening and closing tag lines


def test_grep_no_matches():
    matches, total, truncated = grep_code(SAMPLE_JSX, r"NoSuchThing")
    assert matches == [] and total == 0 and truncated is False


def test_grep_max_matches_cap():
    code = "\n".join("hit" for _ in range(30))
    matches, total, truncated = grep_code(code, r"hit", max_matches=5)
    assert len(matches) == 5 and total == 30 and truncated is True


def test_grep_invalid_regex_raises():
    import re
    with pytest.raises(re.error):
        grep_code(SAMPLE_JSX, r"[unclosed")


def test_grep_context_at_boundaries():
    matches, _, _ = grep_code(SAMPLE_JSX, r"KPI section", before=5, after=1)
    m = matches[0]
    assert m["line_no"] == 1
    assert m["before"] == []  # clamped at start of file
    assert m["context_start_line"] == 1


def test_grep_long_line_clipped():
    code = "x" * 2000
    matches, _, _ = grep_code(code, r"x")
    assert len(matches[0]["line"]) == 500


# ─── build_outline ───────────────────────────────────────────────────────────

def test_outline_page_mode_structure():
    outline = build_outline(SAMPLE_JSX, "page")
    lines = outline.split("\n")
    text = "\n".join(lines)
    assert "     1: // ─── KPI section" in text
    assert "function App()" in text
    assert "SectionCard" in text
    assert "ReactDOM.createRoot" in text
    # Every entry is "  <line_no>: <content>"
    for entry in lines:
        num = entry.split(":")[0].strip()
        assert num.isdigit()


def test_outline_doc_mode_headings():
    md = "# Title\n\nsome text\n\n## Section A\nbody\n### Sub\nmore\nnot a heading"
    outline = build_outline(md, "doc")
    assert "     1: # Title" in outline
    assert "5: ## Section A" in outline
    assert "7: ### Sub" in outline
    assert "not a heading" not in outline


def test_outline_fallback_sampling_when_nothing_structural():
    code = "\n".join(f"plain text line {i}" for i in range(200))
    outline = build_outline(code, "doc")  # no headings → fallback sampler
    assert outline  # non-empty landmarks
    assert "plain text line" in outline


def test_outline_truncation_cap():
    code = "\n".join("# heading" for _ in range(1000))
    outline = build_outline(code, "doc")
    assert "outline truncated at 300 entries" in outline


# ─── threshold sanity ────────────────────────────────────────────────────────

def test_full_read_threshold_constant():
    # A typical dashboard (10-40k chars) must stay on the full-read path.
    assert FULL_READ_MAX_CHARS == 50_000


# ─── observation compaction of windowed payloads ─────────────────────────────

def _read_obs(**extra):
    obs = {
        "summary": "Read artifact 'X' (page, v1, 120,000 chars / 3,000 lines, read_mode=grep)",
        "artifact_id": "a-1",
        "runtime_environment": "SANDBOX DOCS " * 100,
    }
    obs.update(extra)
    return obs


def test_compaction_strips_matches_outline_and_runtime_env():
    builder = ObservationContextBuilder()
    builder.add_tool_observation(
        "read_artifact", {"artifact_id": "a-1", "grep_pattern": "foo"},
        _read_obs(matches=[{"line_no": 1, "line": "foo", "before": [], "after": [], "context_start_line": 1}],
                  outline="     1: function App()"),
        loop_index=1,
    )
    # Same-iteration observations are exempt — matches must survive
    assert "matches" in builder.tool_observations[0]["observation"]

    builder.add_tool_observation(
        "read_artifact", {"artifact_id": "a-1", "offset": 100},
        _read_obs(code="lines 100-500"),
        loop_index=2,
    )
    prev = builder.tool_observations[0]["observation"]
    assert "matches" not in prev
    assert "1 grep matches" in prev["matches_compacted"]
    assert "outline" not in prev
    assert "runtime_environment" not in prev
    assert prev["runtime_environment_compacted"] is True
    # Summary survives so the planner remembers what it saw
    assert "read_mode=grep" in prev["summary"]

    # Latest observation keeps its full payload
    latest = builder.tool_observations[1]["observation"]
    assert latest["code"] == "lines 100-500"


def test_compaction_still_strips_code_for_full_reads():
    builder = ObservationContextBuilder()
    builder.add_tool_observation(
        "read_artifact", {"artifact_id": "a-1"}, _read_obs(code="x" * 1000), loop_index=1,
    )
    builder.add_tool_observation(
        "create_data", {}, {"summary": "made data"}, loop_index=2,
    )
    prev = builder.tool_observations[0]["observation"]
    assert "code" not in prev
    assert prev["code_compacted"] == "1000 chars"
