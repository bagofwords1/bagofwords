# Feedback Loop — "upload filename with ../ can overwrite files", "embed serves uploaded HTML inline", "artifact iframe trusts null-origin messages"

Three security findings, each checked here: reproduced on the old code, then fixed and re-run.

## Root cause (validated)

1. **Path traversal on upload.** In `backend/app/services/file_service.py` (`upload_file`), the stored path was
   `uploads/files/{uuid}_{filename}`, and `filename` is chosen by the client. `storage_safe_name()` only fixes
   text encoding and leaves `/` and `..` alone. `os.makedirs(dirname)` then creates `uploads/files/<uuid>_..`,
   after which each later `..` really climbs upward. `../../x` lands at `uploads/files/x`, without the uuid
   prefix, and overwrites another stored upload. `../../../../x` escapes the store entirely.
2. **Stored XSS on embed.** `backend/app/routes/file.py` (`get_file_embed`) served the bytes with
   `media_type=file.content_type` (whatever the uploader claimed) and `Content-Disposition: inline`, on the
   app's own origin, with no `nosniff` and no CSP. An uploaded `text/html` or `image/svg+xml` file ran as the app.
3. **Forged messages in the MCP artifact iframe.** `frontend/public/mcp-artifact-app.html` accepted
   `event.origin === 'null'`, and every sandboxed frame has that origin. Request ids are sequential. A sibling
   frame could answer `ui/initialize` or `tools/call` first: its reply is written into the page through
   `showError` (`innerHTML`), and a forged `get_artifact_data` reply supplies `data.code` outright.

## Loop A — deterministic reproduction

Backend (from `backend/`):

```bash
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/e2e/test_file_upload_hardening.py -m e2e -q
```

Before the fix, all 14 failed. Uploads ended up in `backend/`, in `uploads/files/<uuid>_../`, and at
non-prefixed names. `text/html`, SVG, XHTML and JS were served `inline` with no `nosniff`.

Iframe (Playwright; serve `frontend/public/mcp-artifact-app.html` at `/` and the harness pages at `/__h/`):

```bash
mkdir -p srv/__h && cp frontend/public/mcp-artifact-app.html srv/ \
  && cp docs/feedback-loops/harness/upload-embed-iframe/*.html srv/__h/
(cd srv && python3 -m http.server 8765 -b 127.0.0.1 &)
node docs/feedback-loops/harness/upload-embed-iframe/run.mjs
```

`attack.html` has a sandboxed sibling frame that floods forged JSON-RPC replies. `legit.html` is a real host
that returns an error containing markup.

Before:
```
attack {"pwned":"1","err":"Initialization failed: Error: "}
legit  {"pwned":"1","err":"Error loading artifact: Error: denied"}
```

## The fix

- `file_service.py`: the client name is cut to its last component (`os.path.basename`, with `\` treated as
  `/`) before the path is built.
- `routes/file.py`: only images (except SVG), PDF, CSV, plain text and JSON are served inline. Everything else
  is sent as an `attachment` with `Content-Security-Policy: default-src 'none'; sandbox`. Every response
  carries `X-Content-Type-Options: nosniff`. Inline types get `script-src 'none'` rather than a full sandbox,
  because Chrome's PDF viewer refuses to render a sandboxed document.
- `mcp-artifact-app.html`: messages are accepted only when `event.source === window.parent`. Error text is
  built with DOM text nodes instead of `innerHTML` (both `showError` and `window.onerror`).
  `mcp-visualization-app.html` had the same origin check and got the same source check.

After:
```
14 passed                                   # test_file_upload_hardening.py
attack {"pwned":null,"err":null}
legit  {"pwned":null,"err":"Error loading artifact: Error: <img src=x onerror=...>denied"}
```

## What this proves / regression notes

- The legitimate host flow still works, and markup in its errors is shown as text.
- `<img src=…/embed>` ignores `Content-Disposition`, so SVG images embedded in artifacts still display.
  Only opening such a file directly in the browser now downloads it.
- Existing file suites still pass: `tests/e2e/rbac/test_file_access.py` and `tests/e2e/test_file*.py`,
  43 passed.
- Not changed: `frontend/public/artifact-sandbox.html` has the same origin pattern, but nothing in the
  frontend loads it.
