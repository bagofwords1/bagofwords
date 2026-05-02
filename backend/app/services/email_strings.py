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
    "fr": {
        NotificationType.SHARE_DASHBOARD: {
            "subject": "{report_title} - Tableau de bord partagé avec vous",
            "heading": "{sender_name} a partagé un tableau de bord avec vous",
            "description": "<strong>{report_title}</strong> a été partagé avec vous.",
            "cta_text": "Voir le tableau de bord",
            "footer": "Envoyé via Bag of Words",
        },
        NotificationType.SHARE_CONVERSATION: {
            "subject": "{report_title} - Conversation partagée avec vous",
            "heading": "{sender_name} a partagé une conversation avec vous",
            "description": "Une conversation de <strong>{report_title}</strong> a été partagée avec vous.",
            "cta_text": "Voir la conversation",
            "footer": "Envoyé via Bag of Words",
        },
        NotificationType.SCHEDULE_REPORT: {
            "subject": "{report_title} - Notification de rapport planifié",
            "heading": "Rapport planifié : {report_title}",
            "description": "{sender_name} a configuré une planification pour <strong>{report_title}</strong>. Vous recevrez des mises à jour à chaque exécution.",
            "cta_text": "Voir le rapport",
            "footer": "Envoyé via Bag of Words",
        },
        SCHEDULED_PROMPT: {
            "subject": "{report_title} - Résultats de la requête planifiée",
            "greeting": "Bonjour,",
            "intro": "Votre rapport planifié « {report_title} » a terminé son exécution.",
            "stats_one_iter": "Il a effectué {iterations} itération.",
            "stats_many_iters": "Il a effectué {iterations} itérations.",
            "stats_one_query": "Il a effectué {queries} requête.",
            "stats_many_queries": "Il a effectué {queries} requêtes.",
            "stats_iters_and_queries_one_one": "Il a effectué {iterations} itération et {queries} requête.",
            "stats_iters_and_queries_one_many": "Il a effectué {iterations} itération et {queries} requêtes.",
            "stats_iters_and_queries_many_one": "Il a effectué {iterations} itérations et {queries} requête.",
            "stats_iters_and_queries_many_many": "Il a effectué {iterations} itérations et {queries} requêtes.",
            "cta_text": "Voir le rapport complet",
            "footer": "— Bag of Words",
        },
    },
    "sv": {
        NotificationType.SHARE_DASHBOARD: {
            "subject": "{report_title} - Instrumentpanel delad med dig",
            "heading": "{sender_name} delade en instrumentpanel med dig",
            "description": "<strong>{report_title}</strong> har delats med dig.",
            "cta_text": "Visa instrumentpanel",
            "footer": "Skickat via Bag of Words",
        },
        NotificationType.SHARE_CONVERSATION: {
            "subject": "{report_title} - Konversation delad med dig",
            "heading": "{sender_name} delade en konversation med dig",
            "description": "En konversation från <strong>{report_title}</strong> har delats med dig.",
            "cta_text": "Visa konversation",
            "footer": "Skickat via Bag of Words",
        },
        NotificationType.SCHEDULE_REPORT: {
            "subject": "{report_title} - Avisering om schemalagd rapport",
            "heading": "Rapport schemalagd: {report_title}",
            "description": "{sender_name} skapade ett schema för <strong>{report_title}</strong>. Du kommer att få uppdateringar när det körs.",
            "cta_text": "Visa rapport",
            "footer": "Skickat via Bag of Words",
        },
        SCHEDULED_PROMPT: {
            "subject": "{report_title} - Resultat av schemalagd prompt",
            "greeting": "Hej,",
            "intro": "Din schemalagda rapport ”{report_title}” har körts klart.",
            "stats_one_iter": "Den genomförde {iterations} iteration.",
            "stats_many_iters": "Den genomförde {iterations} iterationer.",
            "stats_one_query": "Den genomförde {queries} fråga.",
            "stats_many_queries": "Den genomförde {queries} frågor.",
            "stats_iters_and_queries_one_one": "Den genomförde {iterations} iteration och {queries} fråga.",
            "stats_iters_and_queries_one_many": "Den genomförde {iterations} iteration och {queries} frågor.",
            "stats_iters_and_queries_many_one": "Den genomförde {iterations} iterationer och {queries} fråga.",
            "stats_iters_and_queries_many_many": "Den genomförde {iterations} iterationer och {queries} frågor.",
            "cta_text": "Visa hela rapporten",
            "footer": "— Bag of Words",
        },
    },
    "ar": {
        NotificationType.SHARE_DASHBOARD: {
            "subject": "{report_title} - تمت مشاركة لوحة تحكم معك",
            "heading": "{sender_name} شارك معك لوحة تحكم",
            "description": "تمت مشاركة <strong>{report_title}</strong> معك.",
            "cta_text": "عرض لوحة التحكم",
            "footer": "أُرسلت من Bag of Words",
        },
        NotificationType.SHARE_CONVERSATION: {
            "subject": "{report_title} - تمت مشاركة محادثة معك",
            "heading": "{sender_name} شارك معك محادثة",
            "description": "تمت مشاركة محادثة من <strong>{report_title}</strong> معك.",
            "cta_text": "عرض المحادثة",
            "footer": "أُرسلت من Bag of Words",
        },
        NotificationType.SCHEDULE_REPORT: {
            "subject": "{report_title} - إشعار تقرير مُجدوَل",
            "heading": "تقرير مُجدوَل: {report_title}",
            "description": "{sender_name} أعدّ جدولة لـ <strong>{report_title}</strong>. ستتلقى تحديثات عند تشغيله.",
            "cta_text": "عرض التقرير",
            "footer": "أُرسلت من Bag of Words",
        },
        SCHEDULED_PROMPT: {
            "subject": "{report_title} - نتائج المهمة المُجدوَلة",
            "greeting": "مرحبًا،",
            "intro": "انتهى تشغيل تقريرك المُجدوَل «{report_title}».",
            "stats_one_iter": "أكمل تكرارًا واحدًا.",
            "stats_many_iters": "أكمل {iterations} تكرارات.",
            "stats_one_query": "أكمل استعلامًا واحدًا.",
            "stats_many_queries": "أكمل {queries} استعلامات.",
            "stats_iters_and_queries_one_one": "أكمل تكرارًا واحدًا واستعلامًا واحدًا.",
            "stats_iters_and_queries_one_many": "أكمل تكرارًا واحدًا و-{queries} استعلامات.",
            "stats_iters_and_queries_many_one": "أكمل {iterations} تكرارات واستعلامًا واحدًا.",
            "stats_iters_and_queries_many_many": "أكمل {iterations} تكرارات و-{queries} استعلامات.",
            "cta_text": "عرض التقرير الكامل",
            "footer": "— Bag of Words",
        },
    },
    "ru": {
        NotificationType.SHARE_DASHBOARD: {
            "subject": "{report_title} - С вами поделились дашбордом",
            "heading": "{sender_name} поделился с вами дашбордом",
            "description": "<strong>{report_title}</strong> доступен вам.",
            "cta_text": "Открыть дашборд",
            "footer": "Отправлено через Bag of Words",
        },
        NotificationType.SHARE_CONVERSATION: {
            "subject": "{report_title} - С вами поделились беседой",
            "heading": "{sender_name} поделился с вами беседой",
            "description": "Беседа из <strong>{report_title}</strong> теперь доступна вам.",
            "cta_text": "Открыть беседу",
            "footer": "Отправлено через Bag of Words",
        },
        NotificationType.SCHEDULE_REPORT: {
            "subject": "{report_title} - Уведомление о запланированном отчёте",
            "heading": "Запланирован отчёт: {report_title}",
            "description": "{sender_name} настроил расписание для <strong>{report_title}</strong>. Вы будете получать обновления при каждом запуске.",
            "cta_text": "Открыть отчёт",
            "footer": "Отправлено через Bag of Words",
        },
        SCHEDULED_PROMPT: {
            "subject": "{report_title} - Результаты запланированного запроса",
            "greeting": "Здравствуйте,",
            "intro": "Ваш запланированный отчёт «{report_title}» завершил выполнение.",
            "stats_one_iter": "Он выполнил {iterations} итерацию.",
            "stats_many_iters": "Он выполнил {iterations} итераций.",
            "stats_one_query": "Он выполнил {queries} запрос.",
            "stats_many_queries": "Он выполнил {queries} запросов.",
            "stats_iters_and_queries_one_one": "Он выполнил {iterations} итерацию и {queries} запрос.",
            "stats_iters_and_queries_one_many": "Он выполнил {iterations} итерацию и {queries} запросов.",
            "stats_iters_and_queries_many_one": "Он выполнил {iterations} итераций и {queries} запрос.",
            "stats_iters_and_queries_many_many": "Он выполнил {iterations} итераций и {queries} запросов.",
            "cta_text": "Открыть полный отчёт",
            "footer": "— Bag of Words",
        },
    },
    "de": {
        NotificationType.SHARE_DASHBOARD: {
            "subject": "{report_title} - Dashboard mit Ihnen geteilt",
            "heading": "{sender_name} hat ein Dashboard mit Ihnen geteilt",
            "description": "<strong>{report_title}</strong> wurde mit Ihnen geteilt.",
            "cta_text": "Dashboard anzeigen",
            "footer": "Gesendet über Bag of Words",
        },
        NotificationType.SHARE_CONVERSATION: {
            "subject": "{report_title} - Unterhaltung mit Ihnen geteilt",
            "heading": "{sender_name} hat eine Unterhaltung mit Ihnen geteilt",
            "description": "Eine Unterhaltung aus <strong>{report_title}</strong> wurde mit Ihnen geteilt.",
            "cta_text": "Unterhaltung anzeigen",
            "footer": "Gesendet über Bag of Words",
        },
        NotificationType.SCHEDULE_REPORT: {
            "subject": "{report_title} - Benachrichtigung über geplanten Bericht",
            "heading": "Bericht geplant: {report_title}",
            "description": "{sender_name} hat einen Zeitplan für <strong>{report_title}</strong> eingerichtet. Sie erhalten Updates bei jeder Ausführung.",
            "cta_text": "Bericht anzeigen",
            "footer": "Gesendet über Bag of Words",
        },
        SCHEDULED_PROMPT: {
            "subject": "{report_title} - Ergebnisse des geplanten Prompts",
            "greeting": "Hallo,",
            "intro": "Ihr geplanter Bericht „{report_title}“ wurde ausgeführt.",
            "stats_one_iter": "Er hat {iterations} Iteration abgeschlossen.",
            "stats_many_iters": "Er hat {iterations} Iterationen abgeschlossen.",
            "stats_one_query": "Er hat {queries} Abfrage abgeschlossen.",
            "stats_many_queries": "Er hat {queries} Abfragen abgeschlossen.",
            "stats_iters_and_queries_one_one": "Er hat {iterations} Iteration und {queries} Abfrage abgeschlossen.",
            "stats_iters_and_queries_one_many": "Er hat {iterations} Iteration und {queries} Abfragen abgeschlossen.",
            "stats_iters_and_queries_many_one": "Er hat {iterations} Iterationen und {queries} Abfrage abgeschlossen.",
            "stats_iters_and_queries_many_many": "Er hat {iterations} Iterationen und {queries} Abfragen abgeschlossen.",
            "cta_text": "Vollständigen Bericht anzeigen",
            "footer": "— Bag of Words",
        },
    },
    "pt": {
        NotificationType.SHARE_DASHBOARD: {
            "subject": "{report_title} - Painel compartilhado com você",
            "heading": "{sender_name} compartilhou um painel com você",
            "description": "<strong>{report_title}</strong> foi compartilhado com você.",
            "cta_text": "Ver painel",
            "footer": "Enviado via Bag of Words",
        },
        NotificationType.SHARE_CONVERSATION: {
            "subject": "{report_title} - Conversa compartilhada com você",
            "heading": "{sender_name} compartilhou uma conversa com você",
            "description": "Uma conversa de <strong>{report_title}</strong> foi compartilhada com você.",
            "cta_text": "Ver conversa",
            "footer": "Enviado via Bag of Words",
        },
        NotificationType.SCHEDULE_REPORT: {
            "subject": "{report_title} - Notificação de relatório agendado",
            "heading": "Relatório agendado: {report_title}",
            "description": "{sender_name} configurou um agendamento para <strong>{report_title}</strong>. Você receberá atualizações quando for executado.",
            "cta_text": "Ver relatório",
            "footer": "Enviado via Bag of Words",
        },
        SCHEDULED_PROMPT: {
            "subject": "{report_title} - Resultados da solicitação agendada",
            "greeting": "Olá,",
            "intro": "Seu relatório agendado \"{report_title}\" terminou de ser executado.",
            "stats_one_iter": "Ele concluiu {iterations} iteração.",
            "stats_many_iters": "Ele concluiu {iterations} iterações.",
            "stats_one_query": "Ele concluiu {queries} consulta.",
            "stats_many_queries": "Ele concluiu {queries} consultas.",
            "stats_iters_and_queries_one_one": "Ele concluiu {iterations} iteração e {queries} consulta.",
            "stats_iters_and_queries_one_many": "Ele concluiu {iterations} iteração e {queries} consultas.",
            "stats_iters_and_queries_many_one": "Ele concluiu {iterations} iterações e {queries} consulta.",
            "stats_iters_and_queries_many_many": "Ele concluiu {iterations} iterações e {queries} consultas.",
            "cta_text": "Ver relatório completo",
            "footer": "— Bag of Words",
        },
    },
    "it": {
        NotificationType.SHARE_DASHBOARD: {
            "subject": "{report_title} - Dashboard condivisa con te",
            "heading": "{sender_name} ha condiviso una dashboard con te",
            "description": "<strong>{report_title}</strong> è stata condivisa con te.",
            "cta_text": "Visualizza dashboard",
            "footer": "Inviato tramite Bag of Words",
        },
        NotificationType.SHARE_CONVERSATION: {
            "subject": "{report_title} - Conversazione condivisa con te",
            "heading": "{sender_name} ha condiviso una conversazione con te",
            "description": "Una conversazione da <strong>{report_title}</strong> è stata condivisa con te.",
            "cta_text": "Visualizza conversazione",
            "footer": "Inviato tramite Bag of Words",
        },
        NotificationType.SCHEDULE_REPORT: {
            "subject": "{report_title} - Notifica di report pianificato",
            "heading": "Report pianificato: {report_title}",
            "description": "{sender_name} ha impostato una pianificazione per <strong>{report_title}</strong>. Riceverai aggiornamenti a ogni esecuzione.",
            "cta_text": "Visualizza report",
            "footer": "Inviato tramite Bag of Words",
        },
        SCHEDULED_PROMPT: {
            "subject": "{report_title} - Risultati del prompt pianificato",
            "greeting": "Ciao,",
            "intro": "Il tuo report pianificato \"{report_title}\" ha terminato l'esecuzione.",
            "stats_one_iter": "Ha completato {iterations} iterazione.",
            "stats_many_iters": "Ha completato {iterations} iterazioni.",
            "stats_one_query": "Ha completato {queries} query.",
            "stats_many_queries": "Ha completato {queries} query.",
            "stats_iters_and_queries_one_one": "Ha completato {iterations} iterazione e {queries} query.",
            "stats_iters_and_queries_one_many": "Ha completato {iterations} iterazione e {queries} query.",
            "stats_iters_and_queries_many_one": "Ha completato {iterations} iterazioni e {queries} query.",
            "stats_iters_and_queries_many_many": "Ha completato {iterations} iterazioni e {queries} query.",
            "cta_text": "Visualizza il report completo",
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
