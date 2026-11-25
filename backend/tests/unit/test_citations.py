"""Citation markers (``{{cite:<id>}}``) in an answer.

The contract: a marker is stored as written; a surface that cannot render a
source chip gets footnotes or nothing; a marker that names no result of the
report is never shown as a source.
"""
import uuid

import pytest

from app.ai.citations import (
    cited_ids,
    footnote_citations,
    match_citation,
    strip_citations,
)
from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
from app.schemas.ai.planner import PlannerInput
from app.services.platform_adapters.whatsapp_adapter import markdown_to_whatsapp


def _sources(n: int = 3):
    return {str(uuid.uuid4()): f"Result {i}" for i in range(n)}


def test_strip_removes_every_marker_and_leaves_the_sentence_intact():
    ids = list(_sources(2))
    text = f"Revenue grew. {{{{cite:{ids[0][:8]}}}}} Costs fell.{{{{ cite : {ids[1]} }}}}\n\nDone."
    assert strip_citations(text) == "Revenue grew. Costs fell.\n\nDone."


@pytest.mark.parametrize("text", ["", None, "No markers here.", "A {{viz:abc}} embed is not a citation."])
def test_text_without_citations_is_returned_unchanged(text):
    assert strip_citations(text) == text
    assert footnote_citations(text, _sources()) == text
    assert cited_ids(text) == []


def test_a_citation_matches_by_full_id_or_leading_characters_only_when_unambiguous():
    ids = list(_sources(4))
    for candidate in ids:
        assert match_citation(candidate, ids) == candidate
        assert match_citation(candidate[:8], ids) == candidate
        assert match_citation(candidate.upper(), ids) == candidate
    # Too short to identify anything, and an id that is not in the report.
    assert match_citation(ids[0][:5], ids) is None
    assert match_citation(str(uuid.uuid4()), ids) is None
    # Two results sharing the cited prefix: no guess between them.
    twin = ids[0][:8] + str(uuid.uuid4())[8:]
    assert match_citation(ids[0][:8], ids + [twin]) is None


def test_footnotes_number_sources_by_first_mention_and_list_each_once():
    sources = _sources(3)
    a, b, _unused = list(sources)
    text = (
        f"First finding. {{{{cite:{b[:8]}}}}}\n"
        f"Second finding. {{{{cite:{a}}}}}\n"
        f"Back to the first. {{{{cite:{b}}}}}"
    )
    out = footnote_citations(text, sources)
    body, _, footer = out.rpartition("\n\n")
    assert "{{" not in out
    assert body.count("[1]") == 2 and body.count("[2]") == 1 and "[3]" not in body
    # One footer line per cited source, numbered as in the body, naming it.
    lines = footer.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("[1]") and sources[b] in lines[0]
    assert lines[1].startswith("[2]") and sources[a] in lines[1]


def test_a_citation_of_something_not_in_the_report_is_dropped_not_footnoted():
    sources = _sources(2)
    text = f"A claim. {{{{cite:{str(uuid.uuid4())[:8]}}}}}"
    assert footnote_citations(text, sources) == "A claim."
    assert footnote_citations(text, {}) == "A claim."


def test_markers_inside_a_code_fence_are_quoted_text():
    sources = _sources(1)
    vid = next(iter(sources))
    fence = f"```\nwrite {{{{cite:{vid[:8]}}}}} after a sentence\n```"
    assert strip_citations(fence) == fence
    assert footnote_citations(fence, sources) == fence
    assert cited_ids(fence) == []


def test_whatsapp_never_shows_a_raw_marker():
    out = markdown_to_whatsapp(f"Revenue grew **12%**. {{{{cite:{str(uuid.uuid4())[:8]}}}}}")
    assert "cite" not in out and "{{" not in out
    assert "12%" in out


def test_the_planner_is_told_the_marker_grammar_this_module_parses():
    """The prompt and the parser must agree: the example the planner is shown
    has to be a marker ``cited_ids`` recognises."""
    built = PromptBuilderV3.build(PlannerInput(
        user_message="how many orders last month?",
        organization_name="Acme",
        organization_ai_analyst_name="Ada",
    ))
    assert "{{cite:" in built.system
    assert cited_ids(built.system), "the prompt's own example is not a parseable marker"



def test_a_saved_query_materialized_by_describe_entity_is_citable():
    """describe_entity with should_create=True makes a tracked step and
    visualization, like create_data; the web report lists it as a source, so
    the planner must be told it may cite it too."""
    built = PromptBuilderV3.build(PlannerInput(
        user_message="give me the data from the saved query",
        organization_name="Acme",
        organization_ai_analyst_name="Ada",
    ))
    assert "describe_entity" in built.system
    rule = next(line for line in built.system.splitlines() if "Cite your results" in line)
    assert "create_data" in rule and "describe_entity" in rule

@pytest.mark.parametrize("overrides", [
    {"external_platform": "excel"},
    {"mode": "training"},
    {"platform_context": {"surface": "app"}},
])
def test_surfaces_that_cannot_render_a_citation_are_not_asked_for_one(overrides):
    built = PromptBuilderV3.build(PlannerInput(
        user_message="how many orders last month?",
        organization_name="Acme",
        organization_ai_analyst_name="Ada",
        **overrides,
    ))
    assert "{{cite:" not in built.system


# A chat channel is asked to cite only when it cannot render the cited figure
# itself: Teams / Google Chat show the user text only, so the "[1] <title>"
# footnote is their one pointer to the source. WhatsApp / Slack deliver the
# chart as an image in the same turn, so a footnote just repeats a visible title
# (and the "[1]" opens nothing) — they are not asked to cite.
@pytest.mark.parametrize("platform", ["teams", "google_chat"])
def test_channels_that_cannot_render_the_figure_are_asked_to_cite(platform):
    built = PromptBuilderV3.build(PlannerInput(
        user_message="how many orders last month?",
        organization_name="Acme",
        organization_ai_analyst_name="Ada",
        external_platform=platform,
    ))
    assert cited_ids(built.system)


@pytest.mark.parametrize("platform", ["whatsapp", "slack"])
def test_channels_that_show_the_figure_inline_are_not_asked_to_cite(platform):
    built = PromptBuilderV3.build(PlannerInput(
        user_message="how many orders last month?",
        organization_name="Acme",
        organization_ai_analyst_name="Ada",
        external_platform=platform,
    ))
    assert "{{cite:" not in built.system


# --- surfaces that leave the product as plain text ----------------------------

def test_scheduled_run_summary_carries_no_raw_marker():
    """The scheduled-run email/notification renders `last_content` as plain
    markdown: it must never show a `{{cite:…}}` marker."""
    from types import SimpleNamespace
    from app.services.scheduled_prompt_service import ScheduledPromptService

    vid = str(uuid.uuid4())
    block = SimpleNamespace(content=f"Revenue grew 12%. {{{{cite:{vid[:8]}}}}}", tool_execution=None)
    response = SimpleNamespace(completions=[SimpleNamespace(role="system", completion_blocks=[block])])
    summary = ScheduledPromptService._build_execution_summary(None, response)
    assert summary["last_content"] == "Revenue grew 12%."


# --- chat channels ------------------------------------------------------------

@pytest.mark.parametrize("text,org_locale,expected", [
    ("ההכנסות גדלו.", None, "תוצאה"),
    ("Los ingresos crecieron.", "es", "Resultado"),
    ("Revenue grew.", None, "Result"),
])
def test_untitled_footnote_label_follows_the_answer_language(text, org_locale, expected):
    from app.ai.citations import fallback_source_label

    vid = str(uuid.uuid4())
    label = fallback_source_label(text, org_locale)
    assert label == expected
    out = footnote_citations(f"{text} {{{{cite:{vid}}}}}", {vid: ""}, fallback_label=label)
    assert out.endswith(f"[1] {expected}")


class _FailingSession:
    """Stands in for a caller's session: any use of it is a bug."""

    async def execute(self, *a, **k):  # pragma: no cover - must not be called
        raise AssertionError("channel_text must not query the caller's session")


@pytest.mark.asyncio
async def test_channel_lookup_failure_falls_back_without_touching_the_callers_session(monkeypatch):
    """A failed source lookup must not poison the caller's session (the Slack
    send loop keeps using it for the chart and later blocks)."""
    import app.dependencies as deps
    from app.ai.citations import channel_text

    def broken_session_maker():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(deps, "async_session_maker", broken_session_maker)
    vid = str(uuid.uuid4())
    out = await channel_text(_FailingSession(), "report-1", f"Revenue grew. {{{{cite:{vid}}}}}")
    assert out == "Revenue grew."


@pytest.mark.asyncio
async def test_text_without_markers_needs_no_lookup():
    from app.ai.citations import channel_text

    assert await channel_text(_FailingSession(), "report-1", "Plain {{viz:abc}} text.") == "Plain {{viz:abc}} text."


# --- figures: {{show:<id>}} ---------------------------------------------------
# The web report draws the result itself; every other surface has nothing to
# draw, so the marker goes away together with the line it stood on.

def _figure_text(source_id: str) -> str:
    return (
        f"Revenue is concentrated in the USA. {{{{cite:{source_id[:8]}}}}}\n\n"
        f"{{{{show:{source_id[:8]}}}}}\n\n"
        "Canada follows."
    )


def test_strip_removes_a_figure_and_the_line_it_stood_on():
    sid = next(iter(_sources(1)))
    assert strip_citations(_figure_text(sid)) == "Revenue is concentrated in the USA.\n\nCanada follows."


def test_footnotes_keep_citations_and_drop_figures():
    sources = _sources(1)
    sid = next(iter(sources))
    out = footnote_citations(_figure_text(sid), sources)
    assert "{{" not in out
    assert out.startswith("Revenue is concentrated in the USA. [1]\n\nCanada follows.")
    assert out.rstrip().endswith(f"[1] {sources[sid]}")


def test_a_figure_marker_inside_a_code_fence_is_quoted_text():
    text = "Use this marker:\n```\n{{show:3f9a1c2e}}\n```"
    assert strip_citations(text) == text


def _system(**overrides):
    return PromptBuilderV3.build(PlannerInput(
        user_message="revenue by country?",
        organization_name="Acme",
        organization_ai_analyst_name="Ada",
        **overrides,
    )).system


def test_the_web_report_is_told_the_figure_grammar_and_its_limits():
    rule = next(line for line in _system().splitlines() if "{{show:" in line)
    assert "single number" in rule and "At most 3" in rule
    # The planner's own example must be a marker this module parses (and so
    # removes on every surface that has no figure to draw).
    assert "{{show:3f9a1c2e}}" in rule
    assert "{{show:3f9a1c2e}}" not in (strip_citations(rule) or "")
    # The highlight example is a marker the parsers accept too.
    assert 'highlight="USA"' in rule
    assert 'highlight="USA"' not in (strip_citations(rule) or "")


@pytest.mark.parametrize("overrides", [
    {"external_platform": "slack"},
    {"external_platform": "teams"},
    {"external_platform": "excel"},
    {"mode": "training"},
    {"platform_context": {"surface": "app"}},
])
def test_surfaces_without_figures_are_not_asked_for_one(overrides):
    assert "{{show:" not in _system(**overrides)


@pytest.mark.parametrize("marker", ["{{show:3f9a1c2e}}", "`{{show:3f9a1c2e}}`", "(`{{show:3f9a1c2e}}`)", "({{show:3f9a1c2e}})"])
def test_a_figure_named_mid_sentence_leaves_a_clean_sentence(marker):
    assert strip_citations(f"The chart is above {marker}.\n\nNext.") == "The chart is above.\n\nNext."


# --- figures on surfaces without figures: a footnote, not a loss --------------

def test_in_a_channel_a_figure_becomes_a_citation_of_the_paragraph_before_it():
    """The figure can't be drawn in Teams/WhatsApp/Slack, but the reader must
    still learn which result backs the text: the paragraph it followed cites it."""
    sources = _sources(2)
    a, b = list(sources)
    text = f"Revenue is concentrated in the USA.\n\n{{{{show:{a[:8]} highlight=\"USA\"}}}}\n\nCanada follows. {{{{cite:{b[:8]}}}}}"
    out = footnote_citations(text, sources)
    assert "{{" not in out and "highlight" not in out
    assert out.startswith("Revenue is concentrated in the USA. [1]\n\nCanada follows. [2]")
    assert out.rstrip().endswith(f"[1] {sources[a]}\n[2] {sources[b]}")


def test_in_a_channel_a_figure_named_mid_sentence_cites_that_sentence_once():
    sources = _sources(1)
    a = next(iter(sources))
    out = footnote_citations(f"The chart (`{{{{show:{a[:8]}}}}}`) shows it. {{{{cite:{a[:8]}}}}}", sources)
    assert out.startswith("The chart shows it. [1]")
    assert out.count("[1]") == 2  # once in the text, once as the footnote line


def test_a_figure_of_something_not_in_the_report_leaves_no_trace():
    sources = _sources(1)
    out = footnote_citations("Text.\n\n{{show:deadbeef}}\n\nMore.", sources)
    assert out == "Text.\n\nMore."


def test_figure_ids_count_as_cited_so_the_channel_lookup_runs():
    assert cited_ids("Only a figure.\n\n{{show:3f9a1c2e highlight=\"USA\"}}") == ["3f9a1c2e"]


def test_strip_removes_a_figure_with_attributes():
    assert strip_citations('A.\n\n{{show:3f9a1c2e highlight="USA, Canada"}}\n\nB.') == "A.\n\nB."
