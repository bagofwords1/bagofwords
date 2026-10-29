# Feedback Loop — "each mailbox search only returns 25 emails"

A customer scheduled a daily digest: "review all emails in the last 24 hours
(Ops and Telex In folders) and summarise safety, incidents, breakdowns, crew
and commercial issues". The agent reported partial results as complete, missed
a critical incident on the first run, and declared a busy folder "nothing that
needs your attention". It ended by splitting the day into 30-minute → 2-minute
search windows to stay under the cap. The claim validated here: the Outlook
(and Gmail) mail tools could not enumerate a mailbox, a folder, or a time
window completely.

## Root cause (validated)

All in the mail clients and the tools on top of them:

| Gap | Where (before) | Effect |
|---|---|---|
| Hard 25 cap, one page, no `@odata.nextLink` | `graph_mail_client.py` `list_files` / `search_files` (`$top=25`); `gmail_mail_client.py` `maxResults: 25`, `nextPageToken` ignored | Every call returned ≤ 25 rows; nothing told the agent there were more |
| `max_results` dropped | `GraphMailClient.search_files(self, query, **_)` | Asking for 500 still returned 25 |
| `folder_id` ignored | `GraphMailClient.list_files` always hit `/me/messages`; no folder discovery | "Ops" / "Telex In" unreachable; the agent tried KQL `folder:Telex`, which Graph treats as a keyword |
| No unread / date filter | list input was the file-tool schema | Windows could only be approximated with keyword searches |
| Inner quotes break `$search` | the whole query was wrapped in `"…"` without escaping | `breakdown OR "main engine"` → Graph `400 Syntax error: character '"' is not valid` |
| No preview / read state | `_msg_to_item` returned subject/from/date only; Gmail's `snippet` was dropped by `FileEntry` | Summaries written from subject lines |
| "already enumerated" nudge | `note_enumeration` in the shared list/search `_end` | Told the agent to stop re-listing mid-sweep |

## Loop A — deterministic reproduction (no external services)

`backend/tests/unit/test_mail_listing_completeness.py` replaces Graph and Gmail
with fakes at the HTTP boundary that implement the documented semantics:
`$top` default 10 / max 1000, `@odata.nextLink`, `$filter` on `isRead` /
`receivedDateTime`, and the 400s Graph returns for `InefficientFilter`
(`$orderby` property not leading `$filter`), `$search` combined with
`$filter`/`$orderby`, and an unescaped inner quote. Gmail: `pageToken`,
`labelIds`, `is:unread`, `after:` / `before:`.

```bash
cd backend
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db \
  uv run pytest tests/unit/test_mail_listing_completeness.py -q
```

Against the old code (app changes stashed) the suite fails for the reported
reasons, e.g. `assert 25 == 90` on the file-shaped entry point, the quoted
phrase search raising the Graph 400, folder-scoped listings returning Inbox
rows, and the tool output carrying no `truncated` / cursor signal.

## The fix

- `mail_common.py`: opaque cursor encode/decode, ISO date parsing,
  `graph_search_value` (one enclosing quote pair, inner quotes escaped, a
  pre-wrapped query unwrapped), shared default 100 / max 200 per call.
- `GraphMailClient`: `list_mail_folders` walks the folder tree (nested
  `childFolders`, cycle-safe) with unread/total counts; `resolve_mail_folder`
  takes an id, a path (`Inbox/Telex In`) or a unique name and otherwise errors
  with the real folder paths; `list_messages` filters server-side
  (`receivedDateTime` always leads `$filter` so `$orderby` is accepted) and
  `search_messages` applies unread/date to the returned rows (Graph refuses
  `$filter` with `$search`). Both follow
  `@odata.nextLink` and return a cursor (page URL + offset) that resumes
  mid-page; a cursor naming any host other than Graph is rejected so the
  bearer token can't be redirected. Rows carry `preview` (bodyPreview),
  `is_read` and `folder`.
- `GmailMailClient`: labels as folders, `is:unread` / `after:` / `before:`,
  `pageToken` paging with a self-contained cursor, `preview` / `is_read`.
- Tools: `list_emails` / `search_email` take `folder_id`, `unread_only`,
  `received_after`, `received_before`, `max_results`, `cursor`
  (`list_emails(list_folders=true)` returns the folder tree). The
  observation renders every returned row with its preview and states either
  "Complete" or "INCOMPLETE … call again with cursor=…"; the repeat-enumeration
  nudge no longer applies to mail.

Re-run of Loop A after the fix: 42 passed; the related suites
(`test_gmail_mail_client`, `test_graph_connect_regressions`,
`test_graph_mail_test_connection`, `test_message_context_tool_result_projection`,
`test_file_tools`) pass unchanged in behavior — three assertions that pinned
implementation details (`maxResults == "25"`, the list/search tools being
`ListFilesTool`/`SearchFilesTool` subclasses, class-level title attributes) now
assert the behavior instead.

## Loop B — live confirmation (pending)

Not yet run against a real tenant. A delegated token with `Mail.Read` is
needed (the connection's own scope); seeding a test mailbox additionally needs
`Mail.Send`/`Mail.ReadWrite`. Points the fakes encode from documentation and
that a live run should confirm:

1. `GET /me/mailFolders/{id}/childFolders` returns nested folders such as `Inbox/Telex In`.
2. `$filter=receivedDateTime ge … and isRead eq false&$orderby=receivedDateTime desc` is accepted.
3. `$search` results expose `@odata.nextLink` past the first page.
4. `$search="breakdown OR \"main engine\""` (escaped inner quotes) returns 200.

## What this proves / regression notes

Any mailbox size and page size: following the cursor returns every matching
message exactly once, and the cursor is absent only when complete. A daily
digest can now be one `list_emails(folder_id=…, received_after=…)` loop instead
of minute-sized keyword windows.
