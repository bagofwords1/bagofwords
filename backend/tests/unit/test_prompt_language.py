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


def _v3_system(**kwargs) -> str:
    from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
    from app.schemas.ai.planner import PlannerInput

    defaults = dict(
        user_message="hello",
        organization_name="Acme",
        organization_ai_analyst_name="Analyst",
    )
    defaults.update(kwargs)
    return PromptBuilderV3._build_system(PlannerInput(**defaults))


def test_planner_v3_system_prompt_carries_the_language_directive():
    # Regression guard: PlannerV3 is the default planner and its final text IS
    # the user-facing answer, so its system prompt must carry the same
    # mirror-the-user's-language rule as the legacy planner/answer agents.
    # Without it, a Hebrew question against an English system prompt and
    # English schemas gets an English reply.
    system = _v3_system()
    assert "LANGUAGE" in system
    assert "ALWAYS respond in the same language as the user's most recent message" in system


def test_planner_v3_language_fallback_uses_org_locale():
    # The org locale names only the fallback for ambiguous messages; the
    # primary rule still mirrors the user's message.
    system = _v3_system(locale="he")
    assert "Hebrew" in system
    assert "most recent message" in system.lower()
    # No locale -> the system default (English) is the ambiguous fallback.
    assert "default to English" in _v3_system()
