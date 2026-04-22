"""Per-locale email strings for notification templates.

Shape: `STRINGS[locale][notification_type][key] -> str`. Templates receive a
flat `t` dict for the resolved locale, so {{ t.heading }} works in Jinja.

Strings intentionally live here (not in `locales/*.json`) because they are
rendered server-side and the frontend never sees them. Adding a new locale:
copy an existing block and translate in place.

Formatting rules:
- Use `{placeholders}` with the same names the renderer passes in `context`.
- Keep HTML out of these strings; the template handles structure.
- Inline markup allowed only where the context value is already HTML-safe
  (e.g. strong-wrapped titles in `description`); the renderer will not
  escape `description`, so strings like "<strong>{title}</strong>" must come
  from the template layer, not user input.
"""
from __future__ import annotations

from typing import Any

from app.schemas.notification_schema import NotificationType


# Sentinel used when the renderer needs to reference the scheduled-prompt
# pseudo-type. It is not a member of NotificationType, so we key it by string.
SCHEDULED_PROMPT = "scheduled_prompt"


STRINGS: dict[str, dict[Any, dict[str, str]]] = {
    "en": {
        NotificationType.SHARE_DASHBOARD: {
            "subject": "{report_title} - Dashboard shared with you",
            "heading": "{sender_name} shared a dashboard with you",
            "description": "<strong>{report_title}</strong> has been shared with you.",
            "cta_text": "View Dashboard",
            "footer": "Sent via Bag of Words",
        },
        NotificationType.SHARE_CONVERSATION: {
            "subject": "{report_title} - Conversation shared with you",
            "heading": "{sender_name} shared a conversation with you",
            "description": "A conversation from <strong>{report_title}</strong> has been shared with you.",
            "cta_text": "View Conversation",
            "footer": "Sent via Bag of Words",
        },
        NotificationType.SCHEDULE_REPORT: {
            "subject": "{report_title} - Report schedule notification",
            "heading": "Report scheduled: {report_title}",
            "description": "{sender_name} set up a schedule for <strong>{report_title}</strong>. You will receive updates when it runs.",
            "cta_text": "View Report",
            "footer": "Sent via Bag of Words",
        },
        SCHEDULED_PROMPT: {
            "subject": "{report_title} - Scheduled prompt results",
            "greeting": "Hi,",
            "intro": "Your scheduled report “{report_title}” has finished running.",
            "stats_one_iter": "It completed {iterations} iteration.",
            "stats_many_iters": "It completed {iterations} iterations.",
            "stats_one_query": "It completed {queries} query.",
            "stats_many_queries": "It completed {queries} queries.",
            "stats_iters_and_queries_one_one": "It completed {iterations} iteration and {queries} query.",
            "stats_iters_and_queries_one_many": "It completed {iterations} iteration and {queries} queries.",
            "stats_iters_and_queries_many_one": "It completed {iterations} iterations and {queries} query.",
            "stats_iters_and_queries_many_many": "It completed {iterations} iterations and {queries} queries.",
            "cta_text": "View the full report",
            "footer": "— Bag of Words",
        },
    },
    "es": {
        NotificationType.SHARE_DASHBOARD: {
            "subject": "{report_title} - Se ha compartido un panel contigo",
            "heading": "{sender_name} ha compartido un panel contigo",
            "description": "Se ha compartido contigo <strong>{report_title}</strong>.",
            "cta_text": "Ver panel",
            "footer": "Enviado desde Bag of Words",
        },
        NotificationType.SHARE_CONVERSATION: {
            "subject": "{report_title} - Se ha compartido una conversación contigo",
            "heading": "{sender_name} ha compartido una conversación contigo",
            "description": "Se ha compartido contigo una conversación de <strong>{report_title}</strong>.",
            "cta_text": "Ver conversación",
            "footer": "Enviado desde Bag of Words",
        },
        NotificationType.SCHEDULE_REPORT: {
            "subject": "{report_title} - Notificación de informe programado",
            "heading": "Informe programado: {report_title}",
            "description": "{sender_name} ha configurado una programación para <strong>{report_title}</strong>. Recibirás actualizaciones cuando se ejecute.",
            "cta_text": "Ver informe",
            "footer": "Enviado desde Bag of Words",
        },
        SCHEDULED_PROMPT: {
            "subject": "{report_title} - Resultados del informe programado",
            "greeting": "Hola:",
            "intro": "Tu informe programado «{report_title}» ha terminado de ejecutarse.",
            "stats_one_iter": "Completó {iterations} iteración.",
            "stats_many_iters": "Completó {iterations} iteraciones.",
            "stats_one_query": "Completó {queries} consulta.",
            "stats_many_queries": "Completó {queries} consultas.",
            "stats_iters_and_queries_one_one": "Completó {iterations} iteración y {queries} consulta.",
            "stats_iters_and_queries_one_many": "Completó {iterations} iteración y {queries} consultas.",
            "stats_iters_and_queries_many_one": "Completó {iterations} iteraciones y {queries} consulta.",
            "stats_iters_and_queries_many_many": "Completó {iterations} iteraciones y {queries} consultas.",
            "cta_text": "Ver el informe completo",
            "footer": "— Bag of Words",
        },
    },
    "he": {
        NotificationType.SHARE_DASHBOARD: {
            "subject": "{report_title} - לוח בקרה שותף עמך",
            "heading": "{sender_name} שיתף/ה איתך לוח בקרה",
            "description": "<strong>{report_title}</strong> שותף כעת איתך.",
            "cta_text": "לצפייה בלוח הבקרה",
            "footer": "נשלח מאת Bag of Words",
        },
        NotificationType.SHARE_CONVERSATION: {
            "subject": "{report_title} - שיחה שותף עמך",
            "heading": "{sender_name} שיתף/ה איתך שיחה",
            "description": "שיחה מתוך <strong>{report_title}</strong> שותפה איתך.",
            "cta_text": "לצפייה בשיחה",
            "footer": "נשלח מאת Bag of Words",
        },
        NotificationType.SCHEDULE_REPORT: {
            "subject": "{report_title} - התראת תזמון דוח",
            "heading": "תוזמן דוח: {report_title}",
            "description": "{sender_name} הגדיר/ה תזמון עבור <strong>{report_title}</strong>. תקבל/י עדכונים בכל הרצה.",
            "cta_text": "לצפייה בדוח",
            "footer": "נשלח מאת Bag of Words",
        },
        SCHEDULED_PROMPT: {
            "subject": "{report_title} - תוצאות פרומפט מתוזמן",
            "greeting": "שלום,",
            "intro": "הדוח המתוזמן שלך »{report_title}« סיים לרוץ.",
            "stats_one_iter": "הוא השלים איטרציה אחת.",
            "stats_many_iters": "הוא השלים {iterations} איטרציות.",
            "stats_one_query": "הוא השלים שאילתה אחת.",
            "stats_many_queries": "הוא השלים {queries} שאילות.",
            "stats_iters_and_queries_one_one": "הוא השלים איטרציה אחת ושאילתה אחת.",
            "stats_iters_and_queries_one_many": "הוא השלים איטרציה אחת ו-{queries} שאילות.",
            "stats_iters_and_queries_many_one": "הוא השלים {iterations} איטרציות ושאילתה אחת.",
            "stats_iters_and_queries_many_many": "הוא השלים {iterations} איטרציות ו-{queries} שאילות.",
            "cta_text": "לצפייה בדוח המלא",
            "footer": "— Bag of Words",
        },
    },
}


RTL_LOCALES = frozenset({"he", "ar", "fa", "ur"})


def direction_for(locale: str) -> str:
    return "rtl" if locale in RTL_LOCALES else "ltr"


def strings_for(locale: str, notification_type: Any) -> dict[str, str]:
    """Return the strings block for (locale, notification_type), falling back
    to English if the locale isn't registered. Never returns None."""
    lang = STRINGS.get(locale) or STRINGS["en"]
    block = lang.get(notification_type)
    if block is None:
        block = STRINGS["en"].get(notification_type, {})
    return block
