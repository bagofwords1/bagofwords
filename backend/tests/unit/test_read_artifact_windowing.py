"""Unit tests for read_artifact long-artifact windowing: range reads, grep
reads, outline building, and observation compaction of windowed payloads."""

import pytest

from app.ai.tools.implementations.read_artifact import (
    FULL_READ_MAX_CHARS,
    GREP_LINE_CLIP,
    GREP_WINDOW_MIN_AFTER,
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


# ─── grep_code on long lines ─────────────────────────────────────────────────
# Generated JSX often keeps a whole section on one line, with the text being
# looked for thousands of chars in. A long line comes back as a window around
# each occurrence: verbatim, at most GREP_LINE_CLIP chars, and locatable.

def _long_line(length, needles):
    """A line of `length` filler chars with each (col, text) needle placed at col."""
    chars = list("." * length)
    for col, text in needles:
        chars[col:col + len(text)] = text
    return "".join(chars)[:length]


def _assert_window_contract(m, line):
    assert m["line"] == line[m["col_start"]:m["col_end"]]   # verbatim slice
    assert len(m["line"]) <= GREP_LINE_CLIP
    assert m["line_length"] == len(line)


@pytest.mark.parametrize("length,col", [(3172, 2415), (900, 640), (5000, 120), (12000, 9000)])
def test_grep_long_line_window_shows_the_match(length, col):
    line = _long_line(length, [(col, "כל המותגים")])
    code = "function App() {\n" + line + "\n}"
    matches, total, _ = grep_code(code, r"כל המותגים")
    assert total == 1 and len(matches) == 1
    m = matches[0]
    assert m["line_no"] == 2
    assert "כל המותגים" in m["line"]
    _assert_window_contract(m, line)
    assert m["line"] in code  # quotable as an edit find string


def test_grep_long_line_window_near_line_end_stays_full_width():
    line = _long_line(2000, [(2000 - len("END"), "END")])
    m = grep_code(line, r"END")[0][0]
    assert m["line"].endswith("END") and len(m["line"]) == GREP_LINE_CLIP
    _assert_window_contract(m, line)


def test_grep_long_line_window_per_occurrence():
    cols = [700, 1900, 3100]
    line = _long_line(4000, [(c, "<select") for c in cols])
    matches, total, truncated = grep_code(line, r"<select")
    assert total == len(cols) and len(matches) == len(cols) and truncated is False
    for m, col in zip(matches, cols, strict=True):
        assert m["col_start"] <= col < m["col_end"]
        _assert_window_contract(m, line)


def test_grep_long_line_occurrences_sharing_a_window_are_not_repeated():
    line = _long_line(3000, [(1000, "hit"), (1040, "hit"), (1100, "hit")])
    matches, total, _ = grep_code(line, r"hit")
    assert total == 1 and len(matches) == 1
    assert matches[0]["line"].count("hit") == 3


@pytest.mark.parametrize("cols", [(1642, 1952, 2245), (600, 1050, 1100, 1380), (900, 1395)])
def test_grep_long_line_every_occurrence_shown_with_what_follows_it(cols):
    # An occurrence that only clips the tail of the previous window is not
    # "shown": what follows it (the options of a <select>) is what an edit needs.
    line = _long_line(4000, [(c, "<select") for c in cols])
    matches, _, _ = grep_code(line, r"<select")
    for col in cols:
        shown = [m for m in matches if m["col_start"] <= col and col + len("<select") <= m["col_end"]]
        assert shown, col
        assert max(m["col_end"] for m in shown) - (col + len("<select")) >= GREP_WINDOW_MIN_AFTER


def test_grep_long_line_context_rides_on_first_window_only():
    line = _long_line(3000, [(800, "hit"), (2500, "hit")])
    code = "\n".join(["above", line, "below"])
    matches, _, _ = grep_code(code, r"hit", before=1, after=1)
    first, second = matches
    assert first["before"] == ["above"] and first["after"] == ["below"]
    assert first["context_start_line"] == 1
    assert second["before"] == [] and second["after"] == []
    assert second["context_start_line"] == second["line_no"] == 2


def test_grep_max_matches_counts_long_line_windows():
    line = _long_line(6000, [(c, "hit") for c in range(400, 6000, 900)])
    matches, total, truncated = grep_code(line, r"hit", max_matches=3)
    assert total == 7 and len(matches) == 3 and truncated is True


def test_grep_line_at_clip_length_is_returned_whole():
    for length in (GREP_LINE_CLIP - 1, GREP_LINE_CLIP):
        line = _long_line(length, [(length - 3, "hit")])
        m = grep_code(line, r"hit")[0][0]
        assert m["line"] == line
        assert not {"col_start", "col_end", "line_length"} & m.keys()


def _coverage(matches, length):
    """How many returned windows each char of the line appears in."""
    counts = [0] * length
    for m in matches:
        for c in range(m["col_start"], m["col_end"]):
            counts[c] += 1
    return counts


@pytest.mark.parametrize("pattern,line", [
    (r"x", "x" * 2000),                                              # every char matches
    (r"className", 'className="c" ' * 1400),                         # a frequent word
    (r"\b", "שלום עולם " * 400),                                      # zero-width, Hebrew
    (r"", "." * 3000),                                               # empty pattern
])
def test_grep_dense_matches_on_a_long_line_stay_bounded(pattern, line):
    matches, total, _ = grep_code(line, pattern, max_matches=200)
    assert total == len(matches) > 1
    for m in matches:
        _assert_window_contract(m, line)
    # Windows overlap by at most their lead-in: no char is repeated more than twice.
    assert max(_coverage(matches, len(line))) <= 2


def test_grep_long_line_ignore_case():
    line = _long_line(3000, [(2200, "MarketParams")])
    m = grep_code(line, r"marketparams", ignore_case=True)[0][0]
    assert "MarketParams" in m["line"]
    _assert_window_contract(m, line)


def test_grep_long_line_occurrence_longer_than_lead_in_opens_its_window():
    body = "<select>" + "o" * 600 + "</select>"
    line = _long_line(4000, [(1500, body)])
    m = grep_code(line, r"<select>o+</select>")[0][0]
    assert m["col_start"] == 1500  # starts on the occurrence, not 200 chars before it
    _assert_window_contract(m, line)


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
