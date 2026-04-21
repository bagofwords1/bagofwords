"""Locale-aware email rendering.

`render_notification_email` returns `(subject, html)` for a share/schedule
dispatch, and `render_scheduled_prompt_email` returns `(subject, html)` for
the scheduled-prompt result email. Both resolve strings via
`email_strings.strings_for(locale, type)` and render a shared Jinja2
template whose layout is RTL-aware.

Template lookup: `backend/app/templates/emails/*.jinja2`. We construct the
environment lazily so tests/imports don't pay the filesystem cost unless
an email is actually rendered.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.schemas.notification_schema import NotificationType
from app.services.email_strings import (
    SCHEDULED_PROMPT,
    direction_for,
    strings_for,
)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "emails"
_env: Optional[Environment] = None


def _get_env() -> Environment:
    global _env
    if _env is None:
        _env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)),
            autoescape=select_autoescape(["html", "jinja2"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
    return _env


def _escape_user_text(text: str) -> str:
    """Escape user-provided plain text for safe inclusion in HTML.

    Used for the optional `message` field on share emails — this is
    end-user input and must never be rendered as HTML, so we escape it
    ourselves before handing a `| safe` value to the template. Newlines
    become <br> so line breaks survive the email client."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br>")
    )


def _format(template: str, **params) -> str:
    """str.format with missing-key tolerance: leaves {placeholders} intact
    rather than raising, so a misnamed key shows up in staging and not
    as a 500."""
    try:
        return template.format(**params)
    except (KeyError, IndexError):
        return template


def _stats_sentence(t: dict, exec_summary: Optional[dict]) -> str:
    if not exec_summary:
        return ""
    iters = int(exec_summary.get("iterations") or 0)
    queries = int(exec_summary.get("queries") or 0)
    if iters and queries:
        key = (
            "stats_iters_and_queries_"
            + ("one" if iters == 1 else "many")
            + "_"
            + ("one" if queries == 1 else "many")
        )
    elif iters:
        key = "stats_one_iter" if iters == 1 else "stats_many_iters"
    elif queries:
        key = "stats_one_query" if queries == 1 else "stats_many_queries"
    else:
        return ""
    template = t.get(key, "")
    return _format(template, iterations=iters, queries=queries)


def render_notification_email(
    notification_type: NotificationType,
    locale: str,
    *,
    share_url: str,
    report_title: str,
    sender_name: str,
    message: Optional[str] = None,
) -> tuple[str, str]:
    """Render (subject, html) for a share/schedule dispatch email."""
    t = strings_for(locale, notification_type)
    dir_ = direction_for(locale)
    subject = _format(t.get("subject", ""), report_title=report_title, sender_name=sender_name)
    heading = _format(t.get("heading", ""), report_title=report_title, sender_name=sender_name)
    description = _format(t.get("description", ""), report_title=report_title, sender_name=sender_name)
    message_html = _escape_user_text(message) if message else ""

    env = _get_env()
    template = env.get_template("share.html.jinja2")
    html = template.render(
        locale=locale,
        dir=dir_,
        t=t,
        subject=subject,
        heading=heading,
        description=description,
        share_url=share_url,
        message_html=message_html,
    )
    return subject, html


def render_scheduled_prompt_email(
    locale: str,
    *,
    report_title: str,
    report_url: str,
    exec_summary: Optional[dict] = None,
    summary_html: str = "",
) -> tuple[str, str]:
    """Render (subject, html) for a scheduled-prompt result email.

    `summary_html` is pre-rendered HTML (from the caller's markdown->HTML
    conversion). It's trusted to be safe; do not pass user-controlled
    content without sanitization."""
    t = strings_for(locale, SCHEDULED_PROMPT)
    dir_ = direction_for(locale)
    subject = _format(t.get("subject", ""), report_title=report_title)
    intro = _format(t.get("intro", ""), report_title=report_title)
    stats = _stats_sentence(t, exec_summary)
    intro_sentence = f"{intro} {stats}".strip() if stats else intro

    env = _get_env()
    template = env.get_template("scheduled_prompt.html.jinja2")
    html = template.render(
        locale=locale,
        dir=dir_,
        t=t,
        subject=subject,
        intro_sentence=intro_sentence,
        report_url=report_url,
        summary_html=summary_html,
    )
    return subject, html
