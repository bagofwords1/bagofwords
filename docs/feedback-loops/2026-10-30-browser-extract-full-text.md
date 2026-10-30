# Feedback Loop — "browser tools can't persist output as a text file that can be read later"

`execute_mcp` saves every result as a report file the agent can read back with
`read_file`. The built-in browser tools did not: `browser_extract` returned the
page text cut to `max_chars` (max 60k) and dropped the rest, so an answer that
sat past the cut was unreachable. The claim validated here: a long page's full
text is now saved as a report file, the agent gets its `file_id`, and it can
read the rest in the same turn.

## Root cause (validated)

- `backend/app/ai/tools/implementations/browser_extract.py` (before): `text = text[:data.max_chars] + "\n… (truncated)"` — the overflow was discarded, nothing was written.
- Found while building Loop A: `backend/app/ai/tools/implementations/_browser_policy.py` `guard_browser_tool` swaps `ctx["report"]` for a verified `SimpleNamespace` stub (`artifact_verification_policy.py:131`) that has no `files`. A file saved through `save_text_artifact` against that ctx is linked in the DB but not registered in the turn's `report.files`, so `read_file` (`_file_tool_common.py:408 resolve_session_file`) answered "not a file attached to this conversation" in the same turn.

## Loop A — deterministic reproduction (no external services)

`backend/tests/e2e/test_browser_extract_full_text.py` — report, user and browser
connection are seeded through the API; only the browser page is stubbed. It runs
`browser_extract` the way the agent does, then pages the file with `read_file`.

Contracts: long page (3 sizes × 3 `max_chars`) → preview of exactly `max_chars`
+ `text_file_id`, the file read back via `read_file` equals the full text, the
file is linked to the report; a page that fits writes no file; an unchanged page
re-extracted reuses the file, a changed page gets a new one.

```bash
cd backend && BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true \
  uv run pytest tests/e2e/test_browser_extract_full_text.py -m e2e --db=sqlite -q
```

Before (pre-change `browser_extract.py` + `schemas/browser.py`): `5 failed` (`KeyError: 'total_chars'` / `'text_file_id'`).
With the extract change but without the guard fix: `4 failed` — `read_file`: "'…' is not a file attached to this conversation".
After: `5 passed`.

## Loop B — live confirmation (real LLM: claude-haiku-5-5)

`docs/feedback-loops/2026-10-30-browser-extract-full-text.live.py` serves a
4000-row invoice ledger (~107k chars of text) on 127.0.0.1:9123, adds a browser
connection allowlisting it, and asks Haiku 5.5 for invoice INV-03871 (past the
60k inline maximum; expected 544.49). Backend only (`main.py` as in
`tools/agent/boot_stack.sh`); key via `ANTHROPIC_API_KEY`, set `BOW_ENCRYPTION_KEY`
so stored credentials survive a backend restart.

Before:
```
TOOL browser_extract   -> Extracted 60014 chars from http://127.0.0.1:9123/ledger
TOOL browser_act       -> scroll on e3
TOOL browser_extract   -> Extracted 60014 chars ...
TOOL browser_navigate  -> .../ledger?invoice=INV-03871
REPORT FILES []
ANSWER I haven't been able to get the amount for INV-03871 yet. The ledger's text and snapshot both cut off around INV-02239 ...
```

After:
```
TOOL browser_extract -> Extracted 107150 chars ...; the first 60000 are inline and the FULL text is saved as Invoice_ledger.txt, file_id=3b9b7ff2-…
TOOL read_file       -> Read window 60000–68000 of 107150 bytes from 3b9b7ff2-…
TOOL grep_files      -> 1 match(es) in 1/1 file(s) for 'INV-03871'
REPORT FILES [('Invoice_ledger.txt', 'artifact', 'browser_extract:9e99db34…')]
ANSWER 544.49
```

Sandbox note: this sandbox's `/opt/pw-browsers` uses the newer `chrome-linux64/`
layout, which `chromium_executable()` (`_browser_common.py:77`) does not glob, and
the venv's Playwright expects a different build. Loop B pointed
`PLAYWRIGHT_BROWSERS_PATH` at a scratch dir with a `chromium-1243/chrome-linux/chrome`
symlink to the installed binary. Unrelated to this change.

## The fix

- `browser_extract.py`: when the page is longer than `max_chars`, save the full
  text with `save_text_artifact` (same path as `execute_mcp` results;
  `source_kind="artifact"`, `source_ref="browser_extract:<sha1(url+text)>"` for
  reuse), return `text_file_id` / `text_file_name` / `total_chars`, and tell the
  agent to continue with `read_file(offset=max_chars)`. Short pages and
  artifact-preview sessions write nothing. A failed save falls back to the old
  truncated preview.
- `_browser_policy.py`: the guard keeps the agent's own report object as
  `ctx["agent_report"]`; `browser_extract` saves against it when its id matches
  the verified report, so the file is visible to `read_file` in the same turn.
- `schemas/browser.py`: the three new output fields; `max_chars` description.

## What this proves / regression notes

The overflow of a long page is no longer lost: it is in the report, readable in
the same turn and later turns, and a real model uses it unprompted to answer.
`browser_snapshot` is deliberately unchanged (its element refs go stale).

All 43 test files mentioning "browser": 776 passed, 3 failed — the 3
(`test_connection_oauth_flow.py::TestUserOverlaySync` ×2,
`test_connection_file_browse.py::test_contract_sharepoint_server`) fail
identically with this change stashed.
