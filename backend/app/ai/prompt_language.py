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


def build_language_directive(organization_settings: Optional[OrganizationSettingsConfig]) -> str:
    """Return a system-prompt snippet that asks the model to reply in the org locale.

    Returns an empty string when the locale resolves to English so we don't
    waste tokens on the default case.
    """
    locale = resolve_locale(organization_settings)
    if locale == "en":
        return ""
    language = _LOCALE_NAMES.get(locale, locale)
    return (
        f"\n\n**Language**:\n"
        f"Respond to the user in {language}. Translate any English phrasing in "
        f"your narrative prose and headings into {language}. Keep code, SQL, "
        f"column names, identifiers, and JSON field names in English.\n"
    )
