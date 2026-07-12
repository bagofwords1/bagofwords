"""Free-form emails must render right-to-left when their body is written in an
RTL language (Hebrew, Arabic, ...).

The templated share/schedule emails already resolve a locale and set ``dir`` in
their Jinja layout, but the free-form ``send_email`` tool (and the MCP notify
path) send whatever body the agent wrote verbatim. Email clients default to LTR
when no direction is declared, so a Hebrew report rendered LTR: paragraphs
left-aligned and, worst of all, table columns visually flipped.

``EmailSendService`` now auto-detects the body's direction and, for RTL content,
wraps an HTML body in ``<div dir="rtl">`` (and marks a plain-text body with a
leading RLM). These tests assert the invariant — RTL in, RTL wrapper out; LTR in,
no wrapper — not one specific reported string.
"""
import types

import pytest

from app.services.email_send_service import (
    EmailSendService,
    detect_text_direction,
)


# --- the detector: direction follows the dominant strong-directional script ---

@pytest.mark.parametrize(
    "text, is_html, expected",
    [
        ("Q2 revenue was $1.2M, up 14% QoQ.", False, "ltr"),
        ("", False, "ltr"),
        ("12345 — 67.8%", False, "ltr"),  # neutrals only -> ltr
        ("דוח חריגים יומי — 09/07/2026", False, "rtl"),
        ("שלום עולם", False, "rtl"),
        ("مرحبا بالعالم", False, "rtl"),  # Arabic
        # Mixed but Hebrew-dominant (the real report shape: Hebrew prose with
        # scattered English identifiers like HasOverlap / StandardHours).
        ("עובדים: תאיר פרץ (10.28 שעות) HasOverlap StandardHours", False, "rtl"),
        # English-dominant with a stray Hebrew word stays LTR.
        (
            "This is a long English sentence about revenue and growth שלום",
            False,
            "ltr",
        ),
        # HTML tags/attributes/entities must not count as LTR letters and dilute
        # the ratio: a Hebrew table wrapped in English markup is still RTL.
        (
            '<table><tr><td style="text-align:left">שעות לילה</td>'
            "<td>אין חריגות</td></tr></table>",
            True,
            "rtl",
        ),
        # A pure-English HTML table stays LTR.
        (
            "<table><tr><td>Employee</td><td>Hours</td></tr></table>",
            True,
            "ltr",
        ),
    ],
)
def test_detect_text_direction(text, is_html, expected):
    assert detect_text_direction(text, is_html=is_html) == expected


# --- the send path applies the direction wrapper -----------------------------

def _svc():
    return EmailSendService()


def test_apply_direction_wraps_rtl_html():
    svc = _svc()
    body = "<p>שלום, הנה הדוח היומי שלך.</p>"
    out = svc._apply_direction(body, "html")
    assert out.startswith('<div dir="rtl"')
    assert body in out


def test_apply_direction_leaves_ltr_html_untouched():
    svc = _svc()
    body = "<p>Hello, here is your daily report.</p>"
    assert svc._apply_direction(body, "html") == body


def test_apply_direction_marks_rtl_plaintext():
    svc = _svc()
    body = "שלום, הנה הדוח היומי שלך."
    out = svc._apply_direction(body, "plain")
    assert out.startswith("‏")  # RLM
    assert "שלום" in out


def test_apply_direction_leaves_ltr_plaintext_untouched():
    svc = _svc()
    body = "Hello, here is your daily report."
    assert svc._apply_direction(body, "plain") == body


# --- end-to-end through EmailSendService.send() ------------------------------

@pytest.mark.asyncio
async def test_send_wraps_hebrew_html_body(monkeypatch):
    """A Hebrew HTML body handed to send() must reach the SMTP layer wrapped in
    an RTL container, and the appended report-link footer must stay OUTSIDE it
    (URLs render LTR)."""
    captured = {}

    async def _fake_send_custom_email(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(status="sent", error=None)

    from app.services import notification_service as ns_mod
    monkeypatch.setattr(
        ns_mod.notification_service, "send_custom_email", _fake_send_custom_email
    )

    report = types.SimpleNamespace(id="report-123")
    out = await EmailSendService().send(
        db=None,
        recipient="yochze@gmail.com",
        subject="דוח חריגים יומי",
        body="<p>שלום, הנה הדוח היומי שלך.</p><table><tr><td>שעות</td></tr></table>",
        body_format="html",
        report=report,
        organization=None,
        system_completion=None,
    )

    assert out.success is True
    sent_body = captured["body"]
    assert sent_body.startswith('<div dir="rtl"')
    # The report link footer is appended after the RTL wrapper closes, so the
    # deep-link URL is not trapped inside a right-to-left block.
    assert "/reports/report-123" in sent_body
    assert sent_body.index("</div>") < sent_body.index("/reports/report-123")


@pytest.mark.asyncio
async def test_send_leaves_english_html_body_unwrapped(monkeypatch):
    captured = {}

    async def _fake_send_custom_email(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(status="sent", error=None)

    from app.services import notification_service as ns_mod
    monkeypatch.setattr(
        ns_mod.notification_service, "send_custom_email", _fake_send_custom_email
    )

    await EmailSendService().send(
        db=None,
        recipient="yochze@gmail.com",
        subject="Your daily report",
        body="<p>Hello, here is your daily report.</p>",
        body_format="html",
        report=None,
        organization=None,
        system_completion=None,
    )

    assert 'dir="rtl"' not in captured["body"]
