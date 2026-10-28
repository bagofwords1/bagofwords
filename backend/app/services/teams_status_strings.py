"""Per-locale status lines for the Teams 1:1 progress bubble.

Shape: `STRINGS[locale][key] -> str`. Rendered server-side into Teams
informative streaming updates (see `platform_adapters/teams_stream.py`), so
they live here rather than in `locales/*.json`, like `email_strings.py`.

The language follows the user's question (see `status_locale`), matching the
planner's mirror-the-user's-language rule, with the org locale as fallback.
"""
from __future__ import annotations

import re
from typing import Optional

# Tool name -> status key. Anything unlisted shows the generic "working" line.
_TOOL_KEYS = {
    "create_data": "query",
    "run_query": "query",
    "read_query": "query",
    "inspect_data": "inspect",
    "describe_tables": "inspect",
    "describe_entity": "inspect",
    "create_artifact": "dashboard",
    "edit_artifact": "dashboard",
    "create_dashboard": "dashboard",
    "create_widget": "dashboard",
    "create_doc": "doc",
    "edit_doc": "doc",
    "create_instruction": "instructions",
    "edit_instruction": "instructions",
    "web_fetch": "web",
    "browser_navigate": "web",
    "browser_snapshot": "web",
    "browser_extract": "web",
    "browser_act": "web",
    "browser_vision": "web",
}

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "working": "Working on it…",
        "query": "Running a query…",
        "inspect": "Looking at the data…",
        "dashboard": "Building a dashboard…",
        "doc": "Writing a document…",
        "web": "Searching the web…",
        "search": "Searching…",
        "instructions": "Updating instructions…",
        "query_titled": "Running a query: {title}…",
        "dashboard_titled": "Building a dashboard: {title}…",
        "doc_titled": "Writing a document: {title}…",
        "instructions_titled": "Updating instructions: {title}…",
        "still_working": "Still working on it…",
        "done": "Done.",
        "failed": "Something went wrong. Please try again.",
    },
    "es": {
        "working": "Trabajando en ello…",
        "query": "Ejecutando una consulta…",
        "inspect": "Revisando los datos…",
        "dashboard": "Creando un panel…",
        "doc": "Redactando un documento…",
        "web": "Buscando en la web…",
        "search": "Buscando…",
        "instructions": "Actualizando instrucciones…",
        "query_titled": "Ejecutando una consulta: {title}…",
        "dashboard_titled": "Creando un panel: {title}…",
        "doc_titled": "Redactando un documento: {title}…",
        "instructions_titled": "Actualizando instrucciones: {title}…",
        "still_working": "Sigo trabajando en ello…",
        "done": "Listo.",
        "failed": "Algo salió mal. Inténtalo de nuevo.",
    },
    "he": {
        "working": "עובד על זה…",
        "query": "מריץ שאילתה…",
        "inspect": "בודק את הנתונים…",
        "dashboard": "בונה דשבורד…",
        "doc": "כותב מסמך…",
        "web": "מחפש באינטרנט…",
        "search": "מחפש…",
        "instructions": "מעדכן הנחיות…",
        "query_titled": "מריץ שאילתה: {title}…",
        "dashboard_titled": "בונה דשבורד: {title}…",
        "doc_titled": "כותב מסמך: {title}…",
        "instructions_titled": "מעדכן הנחיות: {title}…",
        "still_working": "עדיין עובד על זה…",
        "done": "סיימתי.",
        "failed": "משהו השתבש. נסה שוב.",
    },
    "fr": {
        "working": "Je m'en occupe…",
        "query": "Exécution d'une requête…",
        "inspect": "Analyse des données…",
        "dashboard": "Création d'un tableau de bord…",
        "doc": "Rédaction d'un document…",
        "web": "Recherche sur le web…",
        "search": "Recherche…",
        "instructions": "Mise à jour des instructions…",
        "query_titled": "Exécution d'une requête : {title}…",
        "dashboard_titled": "Création d'un tableau de bord : {title}…",
        "doc_titled": "Rédaction d'un document : {title}…",
        "instructions_titled": "Mise à jour des instructions : {title}…",
        "still_working": "Toujours en cours…",
        "done": "Terminé.",
        "failed": "Une erreur s'est produite. Réessayez.",
    },
    "sv": {
        "working": "Arbetar på det…",
        "query": "Kör en fråga…",
        "inspect": "Tittar på datan…",
        "dashboard": "Bygger en instrumentpanel…",
        "doc": "Skriver ett dokument…",
        "web": "Söker på webben…",
        "search": "Söker…",
        "instructions": "Uppdaterar instruktioner…",
        "query_titled": "Kör en fråga: {title}…",
        "dashboard_titled": "Bygger en instrumentpanel: {title}…",
        "doc_titled": "Skriver ett dokument: {title}…",
        "instructions_titled": "Uppdaterar instruktioner: {title}…",
        "still_working": "Arbetar fortfarande på det…",
        "done": "Klart.",
        "failed": "Något gick fel. Försök igen.",
    },
    "ar": {
        "working": "جارٍ العمل على ذلك…",
        "query": "جارٍ تنفيذ استعلام…",
        "inspect": "جارٍ فحص البيانات…",
        "dashboard": "جارٍ إنشاء لوحة معلومات…",
        "doc": "جارٍ كتابة مستند…",
        "web": "جارٍ البحث على الويب…",
        "search": "جارٍ البحث…",
        "instructions": "جارٍ تحديث التعليمات…",
        "query_titled": "جارٍ تنفيذ استعلام: {title}…",
        "dashboard_titled": "جارٍ إنشاء لوحة معلومات: {title}…",
        "doc_titled": "جارٍ كتابة مستند: {title}…",
        "instructions_titled": "جارٍ تحديث التعليمات: {title}…",
        "still_working": "ما زلت أعمل على ذلك…",
        "done": "تم.",
        "failed": "حدث خطأ ما. حاول مرة أخرى.",
    },
    "ru": {
        "working": "Работаю над этим…",
        "query": "Выполняю запрос…",
        "inspect": "Изучаю данные…",
        "dashboard": "Создаю дашборд…",
        "doc": "Пишу документ…",
        "web": "Ищу в интернете…",
        "search": "Ищу…",
        "instructions": "Обновляю инструкции…",
        "query_titled": "Выполняю запрос: {title}…",
        "dashboard_titled": "Создаю дашборд: {title}…",
        "doc_titled": "Пишу документ: {title}…",
        "instructions_titled": "Обновляю инструкции: {title}…",
        "still_working": "Всё ещё работаю над этим…",
        "done": "Готово.",
        "failed": "Что-то пошло не так. Попробуйте ещё раз.",
    },
    "de": {
        "working": "Ich arbeite daran…",
        "query": "Abfrage wird ausgeführt…",
        "inspect": "Daten werden geprüft…",
        "dashboard": "Dashboard wird erstellt…",
        "doc": "Dokument wird geschrieben…",
        "web": "Suche im Web…",
        "search": "Suche…",
        "instructions": "Anweisungen werden aktualisiert…",
        "query_titled": "Abfrage wird ausgeführt: {title}…",
        "dashboard_titled": "Dashboard wird erstellt: {title}…",
        "doc_titled": "Dokument wird geschrieben: {title}…",
        "instructions_titled": "Anweisungen werden aktualisiert: {title}…",
        "still_working": "Ich arbeite noch daran…",
        "done": "Fertig.",
        "failed": "Etwas ist schiefgelaufen. Bitte versuche es erneut.",
    },
    "pt": {
        "working": "Trabalhando nisso…",
        "query": "Executando uma consulta…",
        "inspect": "Analisando os dados…",
        "dashboard": "Criando um painel…",
        "doc": "Escrevendo um documento…",
        "web": "Pesquisando na web…",
        "search": "Pesquisando…",
        "instructions": "Atualizando instruções…",
        "query_titled": "Executando uma consulta: {title}…",
        "dashboard_titled": "Criando um painel: {title}…",
        "doc_titled": "Escrevendo um documento: {title}…",
        "instructions_titled": "Atualizando instruções: {title}…",
        "still_working": "Ainda trabalhando nisso…",
        "done": "Pronto.",
        "failed": "Algo deu errado. Tente novamente.",
    },
    "it": {
        "working": "Ci sto lavorando…",
        "query": "Esecuzione di una query…",
        "inspect": "Analisi dei dati…",
        "dashboard": "Creazione di una dashboard…",
        "doc": "Scrittura di un documento…",
        "web": "Ricerca sul web…",
        "search": "Ricerca…",
        "instructions": "Aggiornamento delle istruzioni…",
        "query_titled": "Esecuzione di una query: {title}…",
        "dashboard_titled": "Creazione di una dashboard: {title}…",
        "doc_titled": "Scrittura di un documento: {title}…",
        "instructions_titled": "Aggiornamento delle istruzioni: {title}…",
        "still_working": "Ci sto ancora lavorando…",
        "done": "Fatto.",
        "failed": "Qualcosa è andato storto. Riprova.",
    },
}

# Locales whose text uses the Latin script. A Latin-script question can't be
# told apart between these without a language detector, so it takes the org
# locale when that is one of them, and English otherwise.
_LATIN_LOCALES = {"en", "es", "fr", "sv", "de", "pt", "it"}

_SCRIPT_LOCALES = (
    (re.compile(r"[֐-׿]"), "he"),
    (re.compile(r"[؀-ۿ]"), "ar"),
    (re.compile(r"[Ѐ-ӿ]"), "ru"),
)
_LATIN = re.compile(r"[A-Za-zÀ-ɏ]")


def status_locale(text: Optional[str], org_locale: Optional[str]) -> str:
    """Pick the status-line locale from the question's script.

    Hebrew/Arabic/Cyrillic letters decide on their own. Latin letters take the
    org locale if it is Latin-script, else English. No letters at all (a bare
    number, a URL-only message) falls back to the org locale, then English.
    """
    fallback = org_locale if org_locale in STRINGS else "en"
    text = text or ""
    for pattern, locale in _SCRIPT_LOCALES:
        if pattern.search(text):
            return locale
    if _LATIN.search(text):
        return fallback if fallback in _LATIN_LOCALES else "en"
    return fallback


def status_text(locale: str, key: str) -> str:
    strings = STRINGS.get(locale) or STRINGS["en"]
    return strings.get(key) or STRINGS["en"][key]


# Longest title shown on a status line; longer ones are cut (the line already
# ends with "…").
_TITLE_MAX_CHARS = 60


def _short_title(title: Optional[str]) -> str:
    text = " ".join(str(title or "").split())
    return text[:_TITLE_MAX_CHARS].rstrip()


def tool_status_text(locale: str, tool_name: Optional[str], title: Optional[str] = None) -> str:
    """The status line for a tool, naming what it works on when it has a title
    ("Running a query: Customers by country…")."""
    name = tool_name or ""
    key = _TOOL_KEYS.get(name)
    if key is None:
        key = "search" if name.startswith(("search_", "grep_")) else "working"
    short = _short_title(title)
    if short and f"{key}_titled" in STRINGS["en"]:
        return status_text(locale, f"{key}_titled").format(title=short)
    return status_text(locale, key)
