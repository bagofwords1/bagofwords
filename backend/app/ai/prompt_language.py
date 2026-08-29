"""Language directive helper for conversational LLM agents.

Only applied to agents whose output is shown to users in natural language:
planner, answer, judge, reporter, suggest_instructions. Code/artifact agents
(coder, dashboard_designer, excel, doc, data_source) keep English to avoid
breaking identifiers, queries, and structured outputs.
"""
from typing import Optional

from app.schemas.organization_settings_schema import OrganizationSettingsConfig
from app.settings.config import settings


_LOCALE_NAMES = {
    "en": "English",
    "es": "Spanish (Español)",
    "he": "Hebrew (עברית)",
    "fr": "French (Français)",
    "sv": "Swedish (Svenska)",
    "ar": "Arabic (العربية)",
    "ru": "Russian (Русский)",
    "de": "German (Deutsch)",
    "pt": "Brazilian Portuguese (Português do Brasil)",
    "it": "Italian (Italiano)",
}


def resolve_locale(organization_settings: Optional[OrganizationSettingsConfig]) -> str:
    """Return the locale code for this org, falling back to the system default.

    Accepts either the Pydantic ``OrganizationSettingsConfig`` (``.locale``
    attribute) or the ORM ``OrganizationSettings`` model (``.get_config``
    lookup against the JSON config blob). Both shapes are passed around the
    codebase under the name ``organization_settings``.
    """
    default = settings.bow_config.i18n.default_locale
    if organization_settings is None:
        return default
    locale = getattr(organization_settings, "locale", None)
    if not locale and hasattr(organization_settings, "get_config"):
        try:
            locale = organization_settings.get_config("locale")
        except Exception:
            locale = None
    enabled = set(settings.bow_config.i18n.enabled_locales)
    if locale and locale in enabled:
        return locale
    return default


def language_directive_body(locale: Optional[str] = None) -> str:
    """The mirror-the-user's-language rule, without a header.

    ``locale`` is the org locale code used only as the fallback for messages
    too short or ambiguous to detect a language from; None falls back to the
    system default locale. Callers wrap this in whatever header matches their
    prompt's formatting (``build_language_directive`` for markdown-styled
    prompts, PromptBuilderV3 for its caps-header style).
    """
    if not locale:
        locale = settings.bow_config.i18n.default_locale
    fallback = _LOCALE_NAMES.get(locale, locale)
    return (
        f"ALWAYS respond in the same language as the user's most recent message. "
        f"Detect the language from the user's message itself and reply in that "
        f"language. This takes priority over the language of everything else in "
        f"context: tool output, fetched web pages, database rows, table and "
        f"column names, schemas, and any organization instructions written in "
        f"another language. If the user writes in Hebrew, respond in Hebrew, even "
        f"when the data or page content is in English; if the user writes in "
        f"English, respond in English, even when the data or page content is in "
        f"another language. If the user switches languages mid-conversation, "
        f"switch with them. When the user's message is too short or ambiguous to "
        f"determine a language (for example a bare number or a URL), default to "
        f"{fallback}. Always keep code, SQL, column names, identifiers, and JSON "
        f"field names in English."
    )


def build_language_directive(organization_settings: Optional[OrganizationSettingsConfig]) -> str:
    """Return a system-prompt snippet that pins the response language.

    The rule is user-driven: always mirror the language of the user's most
    recent message, regardless of the language of tool output, fetched pages,
    database content, or organization instructions. The org locale is only a
    fallback for messages too short or ambiguous to detect a language from.

    This snippet is always emitted (including for the English default) because
    the failure mode it prevents — the model drifting into the language of
    tool/page/schema content instead of the user's — happens precisely when no
    directive is present.
    """
    locale = resolve_locale(organization_settings)
    return f"\n\n**Language**:\n{language_directive_body(locale)}\n"
