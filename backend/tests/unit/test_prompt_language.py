"""Unit tests for the response-language directive.

The directive must be user-driven: it always instructs the model to mirror the
language of the user's most recent message, overriding the language of tool
output / page content / schemas. Regression guard for the bug where an English
request against a Hebrew page produced a Hebrew reply (and vice versa) because
no directive was emitted for the English default.
"""

from types import SimpleNamespace

from app.ai.prompt_language import build_language_directive, resolve_locale


def test_directive_is_always_emitted_even_for_english_default():
    # None org settings -> resolves to the system default (English), but the
    # directive must still be present so the model has an explicit rule.
    directive = build_language_directive(None)
    assert directive.strip(), "directive must never be empty"
    assert "**Language**" in directive


def test_directive_mirrors_the_user_not_a_fixed_language():
    directive = build_language_directive(None).lower()
    # It anchors on the user's message, not on a single hard-coded language.
    assert "most recent message" in directive
    # It explicitly overrides other context languages.
    assert "priority" in directive
    assert "tool output" in directive


def test_directive_keeps_code_in_english():
    directive = build_language_directive(None).lower()
    assert "code" in directive and "sql" in directive


def test_org_locale_is_only_the_ambiguous_fallback():
    # An explicitly non-English org locale should appear only as the fallback
    # for ambiguous input, not as an override of the user's language.
    he_org = SimpleNamespace(locale="he")
    directive = build_language_directive(he_org)
    if resolve_locale(he_org) == "he":
        # Fallback language named for ambiguous messages...
        assert "Hebrew" in directive
        # ...but the primary rule still mirrors the user.
        assert "most recent message" in directive.lower()
