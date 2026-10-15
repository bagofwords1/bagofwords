# Feedback Loop — "read_file failed: File path is missing or ambiguous"

In a list-filling chat on a SharePoint Server connection, `read_file` failed
with `File path is missing or ambiguous. Use the id returned by
list_files/search_files.` The agent recovered on its next step, but the
message did not say which of the two problems it hit, so the only way forward
was to guess or re-list.

## Root cause (validated)

Reading by a relative path tries that path in every visible document library
and needs exactly one match. Both SharePoint Server clients raised the same
message for zero matches and for several:

* `backend/app/data_sources/clients/_sharepoint_rest.py` — `_resolve`
* `backend/app/data_sources/clients/_sharepoint_2010.py` — `read_raw_bytes`

Not a resolution bug: a unique path reads, and full server paths (the ids that
`list_files` / `search_files` return) always read.

## Loop A — deterministic reproduction (no external services)

The real clients against the in-memory SharePoint REST and SOAP providers the
existing tests use (`tests/unit/test_sharepoint_{onprem,2010}_client.py`).

```bash
cd backend
export BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true
# In sandboxes whose proxy sets a CA bundle, unset it: the synthetic
# providers assert default TLS verification. CI does not set these.
env -u REQUESTS_CA_BUNDLE -u CURL_CA_BUNDLE -u SSL_CERT_FILE \
  uv run pytest -q tests/unit/test_sharepoint_read_path_errors.py
```

Observed on `main` (8701a2b): 8 failed (not-found and ambiguous, both
clients), 2 passed (a unique relative path still reads).

## The fix

`unresolved_path_error` in `sharepoint_onprem_client.py`, used by both clients:

* no match → `File not found: '<path>' does not exist in the connected
  document libraries. Use the id returned by list_files/search_files (the
  full path starting with '/').`
* several → `Ambiguous path: '<path>' matches N files in different document
  libraries: <full path>; <full path>. Read one of them by that full path; it
  is the file id.` (at most 10 listed)

The test reads each listed path to prove the error's suggestion works.

Loop A after the fix: 10 passed. All SharePoint unit tests (2010, modern,
bounded listing): 202 passed.

## What this proves / regression notes

* Full unit suite with the CA override unset: 40 failures with and without
  the change, identical sets. Several of them (OpenAI tool-call ids, query
  timeout, sandbox retries, artifact prompt) are also red in `main`'s CI.
* No live loop: there is no SharePoint Server in the sandbox.
* `documentum_client.py` raises the same combined message; left as is.
