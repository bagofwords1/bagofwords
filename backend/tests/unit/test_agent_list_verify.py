"""Quote verification: a quote counts as verified only when it appears in text
the agent actually read, tolerant of the ways extracted PDF text differs from
what a model quotes — but never of a changed claim."""
import pytest

from app.services.agent_lists.verify import SourceText, normalize, verify_quote


def _src(text, *names):
    return [SourceText("f1", list(names) or ["doc.pdf"], normalize(text))]


@pytest.mark.parametrize("text,quote", [
    ("an annual fee of USD 120,000, excluding VAT", "annual fee of USD 120,000"),              # substring
    ("an annual fee of\nUSD   120,000", "annual fee of USD 120,000"),                          # whitespace / wraps
    ("The pay-\nment is due", "The payment is due"),                                            # hyphenation
    ("the “Customer” shall pay", 'the "Customer" shall pay'),                         # curly quotes
    ("term ends 1–2 June", "term ends 1-2 June"),                                          # dash variants
    ("ANNUAL FEE", "annual fee"),                                                               # case
    ("Fees. The fee is USD 5 and the term is one year.", "The fee is USD 5 ... one year"),      # elided quote
    ("תקופת ההסכם מסתיימת ביום 30 ביוני .2027 ההסכם", "מסתיימת ביום 30 ביוני 2027."),        # RTL punctuation move
    ('בין הספק )"הספק"( לבין', 'בין הספק ("הספק") לבין'),                                     # mirrored brackets
])
def test_equivalent_text_verifies(text, quote):
    assert verify_quote(quote, "doc.pdf", _src(text)) is True


@pytest.mark.parametrize("text,quote", [
    ("an annual fee of USD 120,000", "an annual fee of USD 130,000"),   # different number
    ("renews automatically", "does not renew automatically"),           # negation added
    ("התקופה מסתיימת", "תקופת ההסכם מסתיימת"),                        # paraphrase
    ("anything", "ab"),                                                 # too short to mean anything
])
def test_changed_or_trivial_quotes_do_not_verify(text, quote):
    assert verify_quote(quote, "doc.pdf", _src(text)) is False


def test_quote_from_any_read_source_verifies_even_if_ref_differs():
    sources = _src("irrelevant text", "a.pdf") + _src("the real clause text", "b.pdf")
    assert verify_quote("real clause", "a.pdf", sources) is True
    assert verify_quote("real clause", None, sources) is True
    assert verify_quote("real clause", "x", []) is False
