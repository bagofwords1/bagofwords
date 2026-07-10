# Feedback Loop — "send_email report is not RTL if hebrew"

Reproduces the report: a Hebrew report delivered by the free-form `send_email`
tool renders **left-to-right** in the mail client — paragraphs left-aligned and
table columns visually flipped — because the outgoing HTML carries no direction
hint. Validates that auto-detecting the body's direction and wrapping RTL
content fixes it, without disturbing English (LTR) emails.

## Root cause (validated)

There are two email paths in the backend:

- **Templated** share/schedule/scheduled-prompt emails resolve a locale and set
  `dir` in their Jinja layout via `direction_for(locale)`
  (`backend/app/services/email_strings.py:414-418`). These are already RTL-aware.
- **Free-form** emails — the `send_email` agent tool and the MCP `notify` path —
  send whatever body the model wrote, verbatim. Both funnel through
  `EmailSendService.send()` (`backend/app/services/email_send_service.py:101`;
  the MCP path reaches it via `notify_service._send_email`,
  `backend/app/services/notify_service.py:300-312`). Before the fix, `send()`
  appended a report link (`_append_report_link`) and handed the body straight to
  `notification_service.send_custom_email` with **no direction handling**.

Email clients default to LTR when no `dir` is declared, so a Hebrew body renders
LTR. The `send_email` tool description even instructs the model to write simple
HTML with "no wrapper divs" (`backend/app/ai/tools/implementations/send_email.py:54-62`),
so the model never sets `dir` itself. This is the email in the report — the
`_append_report_link` footer ("Open this in Bag of words:") is its signature.

## Loop A — deterministic reproduction (no external services)

`backend/tests/unit/test_email_rtl_direction.py` — pure detector cases plus an
end-to-end `EmailSendService.send()` run with `send_custom_email` stubbed to
capture the outgoing body.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"; mkdir -p db
uv run pytest tests/unit/test_email_rtl_direction.py -q
```

Observed **FAIL** on pre-fix code — the send path has no direction hook, so the
symbols the test asserts on don't exist:

```
ImportError: cannot import name 'detect_text_direction' from
'app.services.email_send_service'
```

## Loop B — live confirmation (real LLM + real SMTP)

`backend/.repro/rtl_email_e2e.py` (uncommitted; needs `ANTHROPIC_KEY`). A real
model call writes the Hebrew report body — nothing hand-crafted for the assert —
which flows through the real `EmailSendService.send()` → fastapi-mail → a local
`aiosmtpd` sink, then the actual `text/html` MIME part is inspected.

```bash
export BOW_DATABASE_URL="sqlite:///db/app.db"
export ANTHROPIC_KEY=sk-ant-...        # env only; never commit
uv run python .repro/rtl_email_e2e.py
```

Observed on the wire (post-fix), body generated live by the model:

```
First 200 chars of the text/html MIME part actually sent:
<div dir="rtl" style="text-align:right"><p>שלום, להלן דוח החריגים היומי ...
RTL wrapper present at start : True
Report-link footer outside wrapper (LTR): True
RESULT: PASS — real Hebrew email sent with dir="rtl".
```

## The fix

`backend/app/services/email_send_service.py`:

- `detect_text_direction(text, is_html=...)` — counts strong-RTL vs strong-LTR
  letters (RTL Unicode blocks: Hebrew, Arabic, Syriac, Thaana, NKo, …; markup
  stripped for HTML so tag/attribute text can't dilute the ratio). Returns
  `"rtl"` when RTL letters are ≥ 30% of strong letters. Bodies with zero RTL
  letters are always `"ltr"`, so English is never misdetected.
- `EmailSendService._apply_direction(body, subtype)` — for an RTL HTML body,
  wraps it in `<div dir="rtl" style="text-align:right">…</div>` (sets base
  paragraph direction and flips table-column / list order; embedded English runs
  keep their own per-run direction from the client's bidi algorithm). For plain
  text, prefixes a RIGHT-TO-LEFT MARK. LTR bodies pass through untouched.
- Called in `send()` **before** `_append_report_link`, so the LTR deep-link URL
  stays outside the RTL wrapper.

One change covers both the internal `send_email` tool and the MCP `notify` path.

Re-run Loop A after the fix (**PASS**):

```
16 passed
```

## What this proves / regression notes

The loop demonstrates the failure is a missing direction declaration on the
free-form path, not a locale or data problem: RTL body in → `dir="rtl"` wrapper
out; LTR body in → unchanged. The unit test asserts that invariant (not the one
reported Hebrew string) and survives as a regression test. Existing email suites
(`tests/unit/test_notification_email_retry.py`, `tests/e2e/test_email_integration.py`)
still pass — the wrapper only appears for RTL-dominant bodies.
