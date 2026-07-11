"""Report titles must follow the language of the conversation.

The title prompt used to rely solely on the org-locale directive from
``build_language_directive``, which returns an EMPTY string when the org
locale is English (the default). A user chatting in Hebrew on an
English-locale org therefore got English titles ("Pickup Data Analysis
Report") for a Hebrew conversation. The prompt's English-only few-shot
examples reinforced that bias.

This pins the fix: the title prompt always carries an explicit
same-language-as-the-conversation rule, independent of the org locale,
and the conversation itself (in its original language) is embedded in
the prompt so the model can detect the language.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.ai.agents.reporter.reporter import Reporter


def _make_reporter(organization_settings=None):
    with patch("app.ai.agents.reporter.reporter.LLM") as llm_cls:
        llm = MagicMock()
        llm.inference.return_value = "תפוסת מלונות אילת"
        llm_cls.return_value = llm
        reporter = Reporter(model=MagicMock(), organization_settings=organization_settings)
    return reporter, llm


HEBREW_CONVERSATION = 'user: מה התפוסה במלונות אילת בסופ"ש הבא'


@pytest.mark.asyncio
async def test_title_prompt_pins_conversation_language_without_org_locale():
    """Even with no org settings (locale defaults to en, so the org-locale
    directive is empty), the prompt must still instruct the model to write
    the title in the conversation's language."""
    reporter, llm = _make_reporter(organization_settings=None)

    title = await reporter.generate_report_title(HEBREW_CONVERSATION, [])

    prompt = llm.inference.call_args[0][0]
    assert "SAME language" in prompt
    # The conversation is embedded verbatim so the model can see its language.
    assert "מה התפוסה במלונות אילת" in prompt
    # The English few-shots are explicitly marked as shape-only examples.
    assert "examples are in English only" in prompt
    assert title == "תפוסת מלונות אילת"
