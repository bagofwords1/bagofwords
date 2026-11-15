"""The tokenizer and query parser behind the full-text file index.

Documents and queries go through the same normalization, so what matters is
that the two agree: a word typed the way a person would search for it reaches
the form the document was indexed under.
"""
from __future__ import annotations

import io

import pytest

from app.data_sources.clients._fulltext import (
    hebrew_variants,
    parse_query,
    sections_from_byte_stream,
    sections_from_pages,
    sections_from_sheets,
    sections_from_text,
    tokenize,
)


def _forms(text: str) -> set:
    return {f for t in tokenize(text) for f in (t.base, *t.alts)}


def _matches(query: str, document: str) -> bool:
    """A query word matches when any of its forms is among the document's."""
    doc = _forms(document)
    parsed = parse_query(query)
    return bool(parsed.terms) and all(any(f in doc for f in t.forms) for t in parsed.terms)


@pytest.mark.parametrize("query, document", [
    ("48213", "Reference PO-48213 was approved"),          # numbers are kept
    ("po-48213", "Reference PO-48213 was approved"),       # case and joiner
    ("INVOICES", "the invoice was paid"),                   # plural / case
    ("cancelled", "cancelling the order"),                  # -ed / -ing
    ("résumé", "Résumé attached"),                          # accents survive
    ("חוזה", "הפרטים מופיעים בחוזה המצורף"),                # ב + word
    ("בחוזה", "חוזה חדש נחתם"),                             # query carries the particle
    ("ספק", "הודעה תימסר לספק"),                            # ל + word
    ("חוזה", "ובחוזה נקבע אחרת"),                           # two particles
    ("שלום", "שָׁלוֹם רב"),                                  # pointed text
])
def test_query_reaches_the_document_form(query, document):
    assert _matches(query, document)


@pytest.mark.parametrize("query, document", [
    ("48213", "Reference PO-48219 was approved"),
    ("contract", "the contractor arrived"),
    ("ית", "בית גדול"),            # a short native word is not split into a particle
])
def test_unrelated_words_do_not_match(query, document):
    assert not _matches(query, document)


def test_hebrew_variants_keep_at_least_three_letters():
    assert hebrew_variants("בחוזה") == ("חוזה",)
    assert hebrew_variants("ובחוזה") == ("בחוזה", "חוזה")
    assert hebrew_variants("בית") == ()


def test_positions_follow_word_order_so_phrases_can_match():
    terms = tokenize("escrow holdback ceiling")
    assert [t.pos for t in terms] == [1, 2, 3]


def test_quoted_text_and_ids_are_required_phrases_other_joined_words_only_boost():
    q = parse_query('"late delivery" penalty PO-48213 cost/benefit')
    assert ["late", "delivery"] in q.phrases
    assert ["po", "48213"] in q.phrases
    assert ["cost", "benefit"] in q.soft_phrases and ["cost", "benefit"] not in q.phrases
    assert "penalty" in {t.forms[0] for t in q.terms}


def test_stopwords_are_dropped_from_a_question_but_kept_when_nothing_else_is_left():
    q = parse_query("what is the penalty for late delivery")
    words = {t.forms[0] for t in q.terms}
    assert {"penalty", "late", "delivery"} <= words
    assert not {"what", "the", "for"} & words
    assert {t.forms[0] for t in parse_query("the and").terms} == {"the", "and"}


def test_empty_and_punctuation_only_queries_are_empty():
    assert parse_query("").empty
    assert parse_query("?!  --").empty


def test_text_sections_cover_the_whole_text_without_gaps():
    text = ("word " * 900 + "\n") * 5
    sections = sections_from_text(text)
    assert len(sections) > 1
    pos = 0
    for s in sections:
        assert s.kind == "chars" and s.loc["offset"] == pos
        pos += s.loc["length"]
    assert pos == len(text)


def test_byte_sections_point_at_the_bytes_that_hold_their_words():
    lines = [f"line {i} says ünïcödé märker{i:04d} שלום\n" for i in range(400)]
    raw = "".join(lines).encode("utf-8")
    sections, truncated, read = sections_from_byte_stream(io.BytesIO(raw))
    assert not truncated and read == len(raw)
    assert sum(s.loc["length"] for s in sections) == len(raw)
    for s in sections:
        chunk = raw[s.loc["offset"]: s.loc["offset"] + s.loc["length"]]
        # Cut on line ends: every section decodes cleanly on its own.
        text = chunk.decode("utf-8")
        assert {t.base for t in s.terms} == {t.base for t in tokenize(text)}


def test_byte_sections_stop_at_the_cap_and_say_so():
    raw = b"alpha beta gamma\n" * 1000
    sections, truncated, read = sections_from_byte_stream(io.BytesIO(raw), max_bytes=4000)
    assert truncated and read == 4000
    assert sum(s.loc["length"] for s in sections) == 4000


def test_pages_and_sheets_keep_their_location_and_skip_empty_ones():
    pages = sections_from_pages([(1, "first page text"), (2, "   "), (3, "third page " * 800)])
    assert [s.loc["page"] for s in pages][:2] == [1, 3]
    assert all(s.loc["page"] == 3 for s in pages[1:]) and len(pages) > 2   # long page is split
    sheets = sections_from_sheets([("Overrides", "CC-7741,manual"), ("Empty", "")])
    assert sheets[0].loc == {"sheet": "Overrides"}
    # A sheet's name is searchable even when the sheet has no cells.
    assert any("empty" in {t.base for t in s.terms} for s in sheets)
