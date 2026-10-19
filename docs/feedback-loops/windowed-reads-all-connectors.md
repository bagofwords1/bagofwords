# Feedback Loop — `read_file` offset/length paging on every file connector

**Status: executed 2026-10-15 against a synthetic SharePoint 2010 served over
real HTTP plus a network_dir share holding the same documents; one round of
defects found and fixed; second round clean on both connectors. One
pre-existing defect found in chat uploads (not caused here, not fixed here —
see "Found, not fixed").**

Claim being validated: on a connector that has no ranged-read API
(SharePoint Server here; Graph drives, Google Drive, Documentum and the
mailbox/notes clients share the same code path), the agent can read a
document longer than the whole-read observation budget (4,000 characters) by
paging with `offset`/`length` until `eof`, and its answer covers content from
the far end of the document. A connector that pages natively (network_dir,
S3) must keep doing so, except for rich documents (pdf/docx/pptx), whose byte
windows are container bytes and must page as extracted text instead.

Model: Anthropic Claude Haiku 5.5 (`claude-haiku-5-5`), org default. The key
is read from the environment only and never written anywhere.

---

## Loop

1. **Documents.** Three long documents with identical structure and
   different content prefixes: `annual_report_2026.txt` (21,713 chars),
   `strategy_memo_2026.docx` (21,723 chars of extracted text, hand-built
   OOXML) and `board_pack_2026.pdf` (21,703 chars, a hand-built 5-page
   PDF with a Helvetica text layer). Each has 10 sections; section *n*
   carries a unique line `Key figure for this section: code <TAG>-<nn>-<n*1117>`
   (`TXT-`, `DOCX-`, `PDF-`). Sections 3–10 lie past the first 4,000
   characters, so an answer that lists all ten codes correctly proves the
   agent read past the whole-read cap.
2. **Sources.**
   - SharePoint 2010 stand-in: `tests/unit/test_sharepoint_2010_client.py`'s
     `LegacyServer` wrapped in a `ThreadingHTTPServer` on `127.0.0.1:8110`
     (`sharepoint_onprem`, `api_mode: 2010`, `index_mode: content`,
     `auth_policy: system_only`, `allow_http: true`). This client ignores
     `offset`/`length` — the generic fallback is the only route.
   - `network_dir` share with the same files — native byte windows for
     text, extracted-text windows for the docx.
   - A chat upload of the docx (paperclip in the composer).
3. **Stack.** Backend from this worktree (`uvicorn main:app` on 8100,
   pinned `BOW_ENCRYPTION_KEY`), frontend on 3100, org + user + Anthropic
   provider + both connections + data sources seeded through the real API,
   `POST /api/connections/{id}/reindex` polled to `completed` for both.
4. **Chat through the UI (Playwright).** Per source, on one report:
   - "Read the full text of Finance/annual_report_2026.txt and summarise
     each section in order. For every section include its 'Key figure'
     code exactly as written."
   - "Now do the same for Finance/strategy_memo_2026.docx …"
   - (SharePoint only) "And the same for Finance/board_pack_2026.pdf …"
   - (upload) "Read the attached strategy memo in full and list every
     section in order with its 'Key figure' code exactly as written."
5. **Verify below the UI.** `tool_executions` (`tool_name`,
   `arguments_json`, `result_summary`) for every `read_file` call: the
   offsets requested, the window each one returned, whether `eof` was
   reached; `completions` for the final answer; the backend log for real
   `POST https://api.anthropic.com/v1/messages` 200s and no tracebacks.

## Observed — round 1 (SharePoint; the fallback working, two defects)

| Turn | Tool calls recorded (`read_file`, SharePoint) | Outcome |
|---|---|---|
| txt | whole read (`max_chars=100000`) → observation capped at 4,000 chars → **3 windowed reads with `file_id=<session_file_id>` + connection_id → "File path is missing or ambiguous"** → the model retried with the SharePoint path: windows `4000–11913`, `12000–19830`, `20000–21713 (eof)`, all `unit=chars` | All 10 `TXT-` codes correct; the model itself flagged that `11913–12000` and `19830–20000` were never read |
| docx | whole read → windows `4000–12000`, `12000–20000`, `20000–21723 (eof)` | All 10 `DOCX-` codes correct |
| pdf | whole read → windows `4000–12000`, `12000–20000`, `20000–21703 (eof)`; summary carried the `page_range` hint | All 10 `PDF-` codes correct |

Before this change the first windowed call on this connection answered
"This connection does not support windowed (offset/length) reads." and the
agent had no route to characters 4,000–21,713 at all.

| Defect | Root cause | Fix (this PR) |
|---|---|---|
| First paging attempt used the `session_file_id` with the connection id (provider error, one wasted planning step) | The whole-read trailer said "page the rest with windowed reads (offset/length)" right after advertising `session_file_id=…` and never said which id to page with | The trailer now says: re-issue THIS read with the same `file_id` and `connection_id` plus `offset=<chars shown>`, `length<=8000`, not the `session_file_id` |
| Unread gaps `11913–12000` and `19830–20000` in the txt | The model fires fixed-stride windows (4000/12000/20000) **in parallel**, so it cannot chain `next_cursor`; the fallback snapped each window back to the last newline like the native byte windows do, which silently dropped the tail of every window | The character fallback returns exactly `text[offset:offset+length]` — `next_cursor == offset+length`, so fixed-stride paging is contiguous (a split line costs the model nothing; a silent gap costs it the content) |

## Observed — round 2 (after the fixes; SharePoint, network_dir, upload)

| Source / turn | `read_file` calls recorded | Answer |
|---|---|---|
| SharePoint txt | whole read → `4000–12000`, `12000–20000`, `20000–21713 (eof)` — contiguous, `unit=chars`, no errors, no session-id misuse | 10/10 `TXT-` codes correct |
| SharePoint docx | whole read → `4000–12000`, `12000–20000`, `20000–21723 (eof)` | 10/10 `DOCX-` codes correct |
| SharePoint pdf | whole read → `4000–12000`, `12000–20000`, `20000–21703 (eof)` | 10/10 `PDF-` codes correct |
| network_dir txt | whole read → **native** byte windows `4000–11913`, `12000–19830`, `20000–21713 (eof)`, summaries in `bytes` (the native route is untouched) | 10/10 `TXT-` codes correct |
| network_dir docx | whole read → `4000–12000`, `12000–20000`, `20000–21723 (eof)` in `chars` — the document was paged as extracted text although this connector has a byte route | 10/10 `DOCX-` codes correct |
| chat upload docx | `read_file(file_id=<upload id>)` → "Directory path points at a protected system or application directory" (the plain read, before any offset) | no answer — see below |

Backend log for both rounds: every `POST https://api.anthropic.com/v1/messages`
returned 200; no tracebacks.

## Found, not fixed here (pre-existing on `origin/main`)

- **Chat uploads cannot be read by `read_file` at all** when uploads live
  under the backend directory (the default `uploads/files/`):
  `SessionFileClient` builds `NetworkDirClient(root_path="uploads/files")`,
  and `check_network_dir_root` (security hardening bundle #1304) refuses any
  root inside the app checkout or the working directory. Reproduced with
  unchanged client code: `NetworkDirClient(root_path="uploads/files")` from
  `backend/` raises the same error. Out of scope for this PR (the fix belongs
  in `_file_tool_common.SessionFileClient` / the guard, not the windowed
  path); the windowed fallback for uploads is covered by a unit test that
  uses a temp directory instead. Filed as a follow-up task.
- **Native byte windows still leave gaps under parallel fixed-stride
  paging** (network_dir txt: `4000–11913` then `12000–…`): the clients'
  `_read_window` snaps to the last newline. The model got every code right
  here because the dropped tails were paragraph endings, but the same
  behaviour on a log file drops lines. The fix belongs in
  `network_dir_client._read_window` / `s3_client._read_window` (or the model
  guidance to chain `next_cursor` sequentially). Filed as a follow-up task.

## Not verified here

- Real SharePoint Online / Graph, Google Drive, Documentum and mailbox
  connectors (same tool code path, exercised only through the fake client in
  the unit tests).
- A scanned PDF with no text layer paged with `offset` (unit test only: the
  tool answers with the `as_images`/`attach_file` error, not a window).
- `allow_llm_see_data=false` organisations (the window text is withheld from
  the observation exactly as for whole reads; not driven through the UI).

Scratch assets (not in the repo): synthetic server, seeding script,
Playwright scripts, screenshots and `api.jsonl` per run under the session
scratchpad `wl/`.
