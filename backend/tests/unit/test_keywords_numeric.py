"""Keyword extraction keeps id-like tokens from the NAME only."""
from app.data_sources.clients._keywords import extract_keywords


def test_filename_digits_are_keywords_body_digits_are_not():
    kws = extract_keywords("total 12345 and 6789 more", "6044534/appendix-2024.pdf")
    assert "6044534" in kws
    assert "2024" in kws
    assert "appendix" in kws
    assert "12345" not in kws and "6789" not in kws
