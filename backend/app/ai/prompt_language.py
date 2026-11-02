"""Language directive helper for conversational LLM agents.

Only applied to agents whose output is shown to users in natural language:
planner, answer, judge, reporter, suggest_instructions. Code/artifact agents
(coder, dashboard_designer, excel, doc, data_source) keep English to avoid
breaking identifiers, queries, and structured outputs.
"""
from typing import List, Optional

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
        f"ALWAYS respond in the same language as the user's most recent message: "
        f"the words the user typed (for example the text inside <user_prompt>, or "
        f"the newest steering update). Tool results reach you as user-role "
        f"messages but are NOT the user's words, so their language never decides "
        f"yours. Detect the language from the user's message itself and reply in "
        f"that language — progress notes included, not only the final answer. "
        f"This takes priority over the language of everything else in "
        f"context: tool output, fetched web pages, database rows, table and "
        f"column names, schemas, and any organization instructions written in "
        f"another language. If the user writes in Hebrew, respond in Hebrew, even "
        f"when the data or page content is in English; if the user writes in "
        f"English, respond in English, even when the data or page content is in "
        f"another language. If the user switches languages mid-conversation, "
        f"switch with them. When the user's message is too short or ambiguous to "
        f"determine a language (for example a bare number or a URL), default to "
        f"{fallback}. Always keep code, SQL, column names, identifiers, and JSON "
        f"field names in English. For equations use explicit TeX delimiters "
        r"\( ... \) inline or \[ ... \] for display math. "
        "Single dollar signs are reserved for currency; never wrap equations in $...$."
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


# ---------------------------------------------------------------------------
# Per-step reply-language reminder
# ---------------------------------------------------------------------------
#
# The directive above lives in the system prompt, far from where the model
# decides. In a tool loop, every tool result reaches the model as a user-role
# message, so after the first call the newest "user" text is usually English
# SQL/log output and the reply drifts into English. The reminder below rides
# with the newest tool results (the per-step head) and names the language of
# the user's own words, so it is among the last things the model reads.

# Scripts used by essentially one language: safe to name the language. Scripts
# shared by several languages (Latin, Arabic, Cyrillic, Han) are not named —
# the reminder quotes the user's words instead and lets the model match them.
_SINGLE_LANGUAGE_SCRIPTS = (
    ("Hebrew", ((0x0590, 0x05FF), (0xFB1D, 0xFB4F))),
    ("Greek", ((0x0370, 0x03FF), (0x1F00, 0x1FFF))),
    ("Thai", ((0x0E00, 0x0E7F),)),
    ("Korean", ((0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F))),
    ("Japanese", ((0x3040, 0x309F), (0x30A0, 0x30FF))),
)

# Fewer letters than this and the message is too short to carry a language
# ("ok", "2024", a URL is mostly not letters but is handled separately).
_MIN_LETTERS = 4
_SNIPPET_CHARS = 60
_STEERING_OPEN = "<steering_updates>"
_STEERING_CLOSE = "</steering_updates>"


def _latest_user_words(text: str) -> str:
    """The user's newest words in a planner ``user_message``.

    Mid-run steering is appended as an English-framed ``<steering_updates>``
    block; the newest steer, not the framing, is what the user last wrote.
    """
    if not text:
        return ""
    if _STEERING_OPEN in text:
        block = text.split(_STEERING_OPEN, 1)[1].split(_STEERING_CLOSE, 1)[0]
        steers = [ln[2:].strip() for ln in block.splitlines() if ln.startswith("- ") and ln[2:].strip()]
        if steers:
            return steers[-1]
        return text.split(_STEERING_OPEN, 1)[0].strip()
    return text.strip()


def _language_words(text: str) -> str:
    """Text with URLs and inline code dropped — they carry no language."""
    words = []
    in_code = False
    for token in text.replace("```", " ` ").split():
        if token == "`":
            in_code = not in_code
            continue
        if in_code or "://" in token or token.startswith("www."):
            continue
        words.append(token)
    return " ".join(words)


def _letter_count(text: str) -> int:
    return sum(1 for ch in text if ch.isalpha())


def detect_single_language_script(text: str) -> Optional[str]:
    """Name the language when the letters are mostly in a one-language script.

    Returns e.g. "Hebrew" for a Hebrew message (numbers, English table names
    and punctuation mixed in don't change that), None when the script is
    shared by several languages (Latin, Arabic, Cyrillic, Han) or the text is
    too short to tell.
    """
    letters = [ch for ch in _language_words(text) if ch.isalpha()]
    if len(letters) < _MIN_LETTERS:
        return None
    for name, ranges in _SINGLE_LANGUAGE_SCRIPTS:
        hits = sum(1 for ch in letters if any(lo <= ord(ch) <= hi for lo, hi in ranges))
        # Majority, not all: a Hebrew question naming "Invoice.Total" is Hebrew.
        if hits * 2 > len(letters):
            return name
    return None


def _snippet(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) > _SNIPPET_CHARS:
        flat = flat[:_SNIPPET_CHARS].rstrip() + "…"
    return flat.replace('"', "'")


def reply_language_reminder(
    user_message: Optional[str],
    recent_user_messages: Optional[List[str]] = None,
    locale: Optional[str] = None,
) -> str:
    """A one-line ``<reply_language>`` block for the per-step planner head.

    Source of the language, in order: the user's newest words (this turn's
    ask, or the newest mid-run steer); then the newest earlier user message
    that has enough letters (so "ok" or "2024" keeps the conversation's
    language); then the org locale, or the system default.
    """
    candidates = [_latest_user_words(user_message or "")]
    candidates += [m for m in (recent_user_messages or []) if m]

    source = next(
        (c for c in candidates if _letter_count(_language_words(c)) >= _MIN_LETTERS),
        None,
    )
    tail = (
        " Tool results, schemas, data and instructions in other languages never change this."
        " Code, SQL, identifiers and tool arguments stay as they are."
    )
    if source is None:
        if not locale:
            locale = settings.bow_config.i18n.default_locale
        fallback = _LOCALE_NAMES.get(locale, locale)
        return (
            "<reply_language>Write all text meant for the user (progress notes and the final answer) "
            f"in {fallback}: the user's messages are too short to show a language.{tail}</reply_language>"
        )

    named = detect_single_language_script(source)
    quoted = f'"{_snippet(source)}"'
    if named:
        lead = f"in {named}, the language of the user's message {quoted}."
    else:
        lead = f"in the same language as the user's message {quoted}."
    return (
        "<reply_language>Write all text meant for the user (progress notes and the final answer) "
        f"{lead}{tail}</reply_language>"
    )
