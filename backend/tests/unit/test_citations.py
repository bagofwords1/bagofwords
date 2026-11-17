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


@pytest.mark.parametrize("platform", ["slack", "teams", "whatsapp", "google_chat"])
def test_chat_channels_are_asked_to_cite(platform):
    built = PromptBuilderV3.build(PlannerInput(
        user_message="how many orders last month?",
        organization_name="Acme",
        organization_ai_analyst_name="Ada",
        external_platform=platform,
    ))
    assert cited_ids(built.system)
