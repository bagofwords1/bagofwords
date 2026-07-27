"""Unit tests for InstructionContextBuilder scoring + catalog entry helpers.

The scorer moved from Jaccard/one-way-substring (which silently dropped any
instruction with zero exact token overlap) to query-keyword coverage with light
stemming, symmetric containment, and a title/label/table-name boost.
"""
from types import SimpleNamespace

from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder


def _builder() -> InstructionContextBuilder:
    return InstructionContextBuilder(None, SimpleNamespace(id="org"))


# --------------------------------------------------------------------------- #
# _stem
# --------------------------------------------------------------------------- #

def test_stem_variants():
    b = _builder()
    assert b._stem("revenues") == b._stem("revenue") == "revenue"
    assert b._stem("churned") == b._stem("churn") == "churn"
    assert b._stem("cancelling") == b._stem("cancel") == "cancel"
    assert b._stem("companies") == b._stem("company") == "company"
    assert b._stem("matches") == b._stem("match") == "match"
    assert b._stem("sales") == b._stem("sale") == "sale"
    # Short words are left alone
    assert b._stem("es") == "es"


# --------------------------------------------------------------------------- #
# _score_text (coverage)
# --------------------------------------------------------------------------- #

def test_exact_coverage_full_match():
    b = _builder()
    kws = b._extract_keywords("revenue by country")
    assert b._score_text("Revenue is reported by country in USD.", kws) == 1.0


def test_long_document_not_penalized():
    """Jaccard divided by the union of both vocabularies, so long instructions
    could never score well. Coverage only counts unmatched QUERY words."""
    b = _builder()
    long_doc = "revenue " + " ".join(f"word{i}" for i in range(300))
    kws = b._extract_keywords("revenue")
    assert b._score_text(long_doc, kws) == 1.0


def test_stemmed_match():
    b = _builder()
    # 'churned' (query) vs 'churn' (doc): old scorer's one-way substring missed this
    kws = b._extract_keywords("churned customers")
    score = b._score_text("churn rate is computed monthly for each customer", kws)
    assert score > 0.8, score


def test_zero_overlap_scores_zero_but_is_not_dropped_semantics():
    b = _builder()
    kws = b._extract_keywords("marketing spend")
    assert b._score_text("Fiscal year starts in February.", kws) == 0.0


def test_substring_in_joined_words():
    b = _builder()
    kws = b._extract_keywords("invoice")
    assert b._score_text("Use the invoiceline table for line items", kws) >= 0.8


def test_title_boost_ranks_title_match_higher():
    b = _builder()
    kws = b._extract_keywords("refund policy")
    # Body matches only one of the two query words (0.5 coverage); a title hit
    # lifts the combined score above body-only.
    body_only = b._combined_score("the policy is strict", "", kws)
    with_title = b._combined_score("the policy is strict", "Refund policy", kws)
    assert 0 < body_only < 1.0
    assert with_title > body_only


def test_table_name_extra_text_matches():
    """A query naming a table should match an instruction scoped to that table
    even when the word never appears in its prose."""
    b = _builder()
    kws = b._extract_keywords("show invoices per region")
    inst = SimpleNamespace(
        text="Amounts are stored in cents; divide by 100.",
        title=None, description=None, formatted_content=None,
        structured_data=None, labels=[],
    )
    without_refs = b._score_instruction(inst, kws)
    with_refs = b._score_instruction(inst, kws, extra_text="invoices")
    assert without_refs == 0.0
    assert with_refs > 0.0


# --------------------------------------------------------------------------- #
# _catalog_entry (140-char fallback)
# --------------------------------------------------------------------------- #

def test_catalog_entry_titled_uses_title_and_description():
    b = _builder()
    e = b._catalog_entry(
        inst_id="a" * 36, title="Revenue definition",
        description="How revenue is computed.", structured_data=None,
        text="LONG BODY should not appear", table_refs=["invoices"], usage_count=7,
    )
    assert e.title == "Revenue definition"
    assert e.description == "How revenue is computed."
    assert e.table_refs == ["invoices"]
    assert e.usage_count == 7
    assert e.short_id == "a" * 8


def test_catalog_entry_untitled_shows_140_char_snippet_once():
    b = _builder()
    body = ("Cancelled orders must be excluded from all KPI computations "
            "including revenue, average order value, and\nconversion.  Extra "
            "detail that goes beyond the snippet cap and should be truncated away entirely.")
    e = b._catalog_entry(
        inst_id="b" * 36, title=None, description=None, structured_data=None,
        text=body, table_refs=[], usage_count=None,
    )
    # Whitespace collapsed, capped at CATALOG_SNIPPET_LEN, no duplicate description
    assert "\n" not in e.title
    assert len(e.title) <= InstructionContextBuilder.CATALOG_SNIPPET_LEN
    assert e.title.startswith("Cancelled orders must be excluded")
    assert e.title.endswith("…")
    assert e.description is None


def test_catalog_entry_titled_falls_back_to_first_line_description():
    b = _builder()
    e = b._catalog_entry(
        inst_id="c" * 36, title="T", description=None, structured_data=None,
        text="First line here.\nSecond line.", table_refs=[], usage_count=None,
    )
    assert e.description == "First line here."
