# Feedback Loop — "I can't tell which sheets the Excel has"

**Status: executed 2026-10-30 on a network_dir share with a mock multi-sheet
workbook; FAIL on the original code, PASS after the fix. No live LLM in this
sandbox (no provider key), so the agent loop was driven at the tool boundary.**

Report (Google Drive, Hebrew UI): the user asked *"מה אתה רואה באקסל? איזה
גיליונות יש בו?"* about `gindi_mavat_kiryat_ono.xlsx`. The agent described
the summary page and then said it could not tell the sheet names: "the
connection returned the first sheet's content as CSV, without the list of
sheets". The claim under test: this is not Drive-specific. Every file
connector reads one sheet and drops the workbook's sheet list, so the model
can never learn other sheets exist.

## Root cause (validated)

1. **Every connector read one sheet and threw the sheet list away:**
   `pd.read_excel(..., sheet_name=sheet or 0)` in
   `google_drive_client.py:393` (and `:358` for native Google Sheets),
   `graph_drive_client.py:1214` (SharePoint Online / OneDrive),
   `sharepoint_onprem_client.py:285`, `documentum_client.py:535`,
   `network_dir_client.py:567`, `s3_client.py:382`,
   `servicenow_client.py:824` (which also ignored `sheet` entirely).
2. **The tool rendered it as CSV with no sheet info:**
   `render_file_payload` (`_file_tool_common.py:609`) serialized the frame to
   `csv`; the observation summary said `N rows × M cols` and nothing else.
   The `sheet` input existed but the model had no names to pass.
3. **Found while fixing, would have bitten as soon as the model used
   `sheet`:**
   - the content cache was keyed on `(connection, file_id, version)` only
     (`read_file.py:755`), so `sheet="X"` after a default read was served
     the cached **first** sheet (see the BEFORE output below);
   - the session file was keyed on `source_ref=file_id`
     (`read_file.py` `_persist_session_file`), so reading sheet 2 would have
     overwritten the session file the model already had for sheet 1.

The search index was never affected: content indexing already walks every
sheet and includes sheet names (`s3_client.py:599`,
`network_dir_client.py:615`, `sharepoint_onprem_client.py:333`). So search
could match text on sheet 3 that a read could not reach.

## Loop A — deterministic (no external services)

Mock workbook: three sheets with Hebrew/space/numeric-looking names
(`סיכום`, `תוכניות` with 528 rows, `גושים וחלקות` in the e2e; `סיכום`,
`Plans 2026`, `2024` in the unit tests), written with openpyxl into a temp
network_dir root.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
# the sandbox's proxy CA env vars break unrelated requests-session asserts
env -u REQUESTS_CA_BUNDLE -u CURL_CA_BUNDLE -u SSL_CERT_FILE \
  uv run pytest -q tests/unit/test_read_file_workbook_sheets.py
env -u REQUESTS_CA_BUNDLE -u CURL_CA_BUNDLE -u SSL_CERT_FILE \
  uv run pytest -q -m e2e --db=sqlite tests/e2e/test_network_dir_workbook_sheets.py
```

- `tests/e2e/test_network_dir_workbook_sheets.py`: real Connection /
  DataSource / Report rows, real client resolution, real content cache and
  session `File` rows; drives `ReadFileTool` as the agent loop does.
- `tests/unit/test_read_file_workbook_sheets.py`: the same contract through
  `ReadFileTool` + a real `NetworkDirClient`, plus each connector's own read
  path with only the provider boundary faked (Graph `_get`/`_get_bytes`,
  Google Drive `_get`/`_get_bytes` for both an uploaded `.xlsx` and a native
  Sheet, S3 via botocore `Stubber`).

**BEFORE** (fix stashed), what the model receives:

```
{} -> summary: Read gindi.xlsx — tabular — 1 rows × 2 cols
   sheets: None sheet: None
{'sheet': 'Sheet2'} -> summary: Read gindi.xlsx — tabular — 1 rows × 2 cols — cached
   sheets: None sheet: None          ← a sheet that doesn't exist, served sheet 1 from cache
```

```
tests/e2e/test_network_dir_workbook_sheets.py
>           assert out["sheets"] == list(SHEETS)
E           KeyError: 'sheets'
1 failed
```

## The fix

- **One shared reader:** `read_excel_sheet()` in
  `data_sources/clients/_file_source_common.py`. It opens the workbook once,
  resolves `sheet` (exact match, then case/whitespace-insensitive), and tags
  the frame with `attrs["sheet_names"]` / `attrs["sheet"]`. An unknown name
  raises `SheetNotFoundError` listing the real sheets. `trim_to_data` moved
  here as well; `graph_drive_client._trim_to_data` stays as an alias.
  `.xlsm` is now read as a workbook too, matching `routes/data_source.py`.
- **All connectors use it:** Google Drive (uploaded xlsx, and native Sheets:
  the xlsx-export path, plus a `sheets.properties.title` metadata call on the
  Sheets-API path), SharePoint Online/OneDrive, SharePoint on-prem,
  Documentum, network_dir, S3, ServiceNow (which now honors `sheet`). Header
  handling is unchanged per connector (`trim=False` where they used pandas'
  default header row).
- **Tool layer:** `render_file_payload` emits `sheet` + `sheets`
  (`ReadFileOutput` fields). The observation summary names the sheet and
  lists every sheet, with how to read another (capped at 50 names; the full
  list stays in `sheets`). The `sheet` input description says so.
- **Per-sheet keys:** `_sheet_keys()` in `read_file.py` gives a named-sheet
  read its own cache key and session-file `source_ref`
  (`<file_id>#sheet=<name>`, display name `<file> - <sheet>.csv`). A default
  read keeps the bare file id, so existing session files stay valid. The
  cache schema went to `3`, because old tabular entries lack `sheets`.

**AFTER:**

```
{} -> summary: Read gindi.xlsx — tabular — 1 rows × 2 cols — sheet "סיכום" (1 of 3);
      workbook sheets: "סיכום", "תוכניות", "גושים וחלקות" — read another with the
      same file_id plus sheet="<name>"
   sheets: ['סיכום', 'תוכניות', 'גושים וחלקות'] sheet: סיכום
{'sheet': 'Sheet2'} -> read_file failed: Sheet "Sheet2" not found. This workbook has
      3 sheet(s): "סיכום", "תוכניות", "גושים וחלקות". Pass one of these names as `sheet`.
```

```
tests/unit/test_read_file_workbook_sheets.py   17 passed
tests/e2e/test_network_dir_workbook_sheets.py   1 passed
```

Each test can fail. With only the tool-layer pieces disabled
(`render_file_payload` not emitting sheets, cache/session keys not
per-sheet), 7 of the 17 unit tests fail: every `ReadFileTool`-level one.
The 10 that pass are the connector/reader tests, which hold independently.

## What this proves / regression notes

- On any file connector, a workbook read now tells the model every sheet
  name in the observation it reads, and a follow-up `sheet=` read returns
  that sheet, caches separately, and gets its own session file.
- Not verified live: an LLM actually following the hint (no provider key in
  this sandbox), and the real Google/Graph/S3 APIs (the boundaries are faked
  in Loop A). Loop B, when a key is available: seed a network_dir connection
  with this workbook via `tools/agent/seed_org.py`, ask "what sheets does
  this Excel have?", and expect a `read_file` call followed by an answer that
  names all three sheets, and a second `read_file` with `sheet=` on a
  follow-up.
- Regression runs: the file-connector unit suites (675 passed, same as with
  the fix stashed) and related e2e suites (119 passed). The full `tests/unit`
  shows 32 failures (OBO login, discovery contract, Sybase). They fail
  identically with the fix stashed, because `libodbc.so.2` is missing in
  this sandbox, so they are unrelated.
- Out of scope, unchanged: legacy `.xls` uses the same reader via xlrd but
  has no fixture here (no xls writer installed); sheets are still read one
  at a time (no "read all sheets" mode).
