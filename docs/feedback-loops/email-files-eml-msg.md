# Feedback Loop — email files (.eml / .msg) as first-class documents

Validates that an email file stored in ANY file-based source (a SharePoint
Server 2010 library over real SOAP/HTTP, a network directory, a chat upload)
is readable with `read_file`, searchable by terms that live only inside its
attachments, and that its attachments are readable as sub-files through the
`<email id>#att:<index>:<filename>` id — driven through the real UI with a
real LLM (Claude Haiku 5.5) and verified in `tool_executions`,
`connection_tables`, `files` and the backend log.

Feature branch: `ai/email-files-eml-msg`. Core module:
`backend/app/data_sources/clients/_email_text.py`.

## What was added (recap)

- `.eml` (stdlib `email`, `policy.default`) and `.msg` (extract-msg → MIME
  tree built from the parsed MAPI object) render to one flat text: header
  block → `Attachments (N)` list with a sub-file id per attachment (before
  the body, so the capped observation the planner reads always carries the
  ids) → body (text/plain preferred, HTML stripped) → each attachment's own
  text (documents via the shared extractor, tabular/text decoded, nested
  messages rendered inline — transfer-encoded `message/rfc822` parts are
  decoded first — depth cap 3).
- `eml`/`msg` joined `DOC_EXTS`, so every connector's "rich document"
  branch (network_dir, S3, Graph/OneDrive/SharePoint Online, SharePoint
  Server, Google Drive, Documentum) reads, indexes and searches them with no
  per-connector code; network_dir/S3 grep over the rendered text.
- `read_file` resolves `<parent>#att:<i>:<name>` through the PARENT's own
  read path (scope/permission checks included) and then the normal typed
  read (`EmailAttachmentClient` in `_file_tool_common.py`); `attach_file`
  and the document-preview path take the same ids via `read_source_bytes`.

## Loop A — deterministic (no LLM, no services)

```bash
cd backend
env -u REQUESTS_CA_BUNDLE TESTING=true BOW_DATABASE_URL=sqlite:///db/unit.db \
  uv run pytest tests/unit/test_email_files.py tests/unit/test_sharepoint_2010_email.py \
  tests/e2e/test_email_attachments_index_search.py -m "e2e or not e2e" --db=sqlite -q
# -> 43 passed
```

`.msg` fixtures are REAL Outlook files written by
`backend/tests/unit/_msg_fixture.py` (a minimal [MS-CFB] + [MS-OXMSG] writer)
and parsed by extract-msg, so the `.msg` path is exercised, not mocked. Every
new test was shown to fail with the feature broken (see the PR body).

## Loop B — live stack from this worktree

Ports chosen not to collide with the stacks on 8000/3000/8010 and 8100/3100/8110.

| Component | How |
|---|---|
| SharePoint 2010 | `scratch/myfl/sp2010_server.py` — the unit-test `LegacyServer` behind a real `ThreadingHTTPServer` on `127.0.0.1:8210`, serving the fixtures below from `/sites/research/Shared Documents` |
| network_dir | the same fixtures written to `scratch/myfl/netdir/` |
| Backend | `uv run alembic upgrade head` then `uv run uvicorn main:app --port 8200` with `BOW_DATABASE_URL=sqlite:///db/app.db`, a pinned `BOW_ENCRYPTION_KEY`, `BOW_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium` |
| Frontend | `PORT=3200 BOW_API_TARGET=http://127.0.0.1:8200 yarn dev` (node_modules symlinked from the main checkout, `public/libs` copied) |
| Seeding | `POST /api/auth/register` → `/api/auth/jwt/login` → `/api/organizations` → `/api/llm/providers` (Anthropic, `claude-haiku-5-5`) → two `/api/connections` + `/api/data_sources` (`Email Library` = sharepoint_onprem 2010, `index_mode=content`; `Mail Folder` = network_dir, `index_mode=content`) → `/api/connections/{id}/reindex`, polled to `completed` (7/7 files each) |
| Driver | Playwright (`/opt/pw-browsers/chromium`): `01_login.js` (sign-in, skip onboarding, storageState), `02_chat.js <agent> <tag> <prompts…>` (agent picker in the composer, `[contenteditable]` input, send = last `button[class*="rounded-full"]`, wait for no `[data-testid="stop-button"]`), `03_upload.js` (paperclip → hidden `input[type=file]` → prompts) |

### Fixtures (one term per layer, so a hit proves WHICH layer was indexed)

| File | Shape | Unique terms |
|---|---|---|
| `Inbox/Quarterly numbers.eml` | plain body, Cc, `report.pdf` + `data.csv` attachments | body: *zebrafish*; PDF only: *pangolin*; CSV: widget 10 / gadget 32 / sprocket 58 |
| `Inbox/Vendor onboarding.eml` | HTML-only body | *okapi* |
| `Inbox/דוח רבעוני.eml` | Hebrew subject (RFC 2047) + windows-1255 body | Hebrew text |
| `Inbox/FW Vendor shortlist.eml` | nested forwarded message with its own `vendors.csv` | nested body: *narwhal* |
| `Inbox/Outlook quote.msg` | real Outlook file: body, `invoice.csv`, embedded `.msg` | *axolotl*, *marmoset*; embedded "Original request" |
| `Finance/revenue.csv`, `Finance/budget_2026.txt` | non-email control files | — |

### Index layer (`connection_tables`, after reindex)

```
type              | name                           | pangolin(PDF) | zebrafish(body) | narwhal(nested) | marmoset(msg body)
network_dir       | Inbox/Quarterly numbers.eml    | 1 | 1 | 0 | 0
network_dir       | Inbox/FW Vendor shortlist.eml  | 0 | 0 | 1 | 0
network_dir       | Inbox/Outlook quote.msg        | 0 | 0 | 0 | 1
sharepoint_onprem | Inbox/Quarterly numbers.eml    | 1 | 1 | 0 | 0
sharepoint_onprem | Inbox/FW Vendor shortlist.eml  | 0 | 0 | 1 | 0
sharepoint_onprem | Inbox/Outlook quote.msg        | 0 | 0 | 0 | 1
```

Every email row carries `mime_type` (`message/rfc822` / `application/vnd.ms-outlook`
— the latter registered by `_email_text`, Python's table has none) and
`indexed: true`; a term that exists only inside an attached PDF is a keyword
of the EMAIL's row on both connectors.

### Round 1 — SharePoint 2010 agent ("Email Library"), one report, three turns

Prompts and what the agent did (`tool_executions`, in order):

| Turn | Prompt | Tool calls (arguments → result) |
|---|---|---|
| 1 | *Who sent the email about quarterly numbers and what did they ask?* | `search_files(query="quarterly numbers")` → 1 hit from the keyword index; `read_file(file_id="/sites/research/Shared Documents/Inbox/Quarterly numbers.eml")` → text, session file attached |
| 2 | *Which emails have attachments mentioning pangolin?* | `grep_files` → refused (SharePoint Server declares no grep capability — pre-existing); `search_files(query="pangolin", deep=true)` → 1 hit (keyword index + live scan) |
| 3 | *Open the CSV attached to the quarterly numbers email and total the amount column.* | `read_file(file_id="…/Inbox/Quarterly numbers.eml#att:1:data.csv")` → tabular, 3 rows × 2 cols, session file attached |

Answers (from the rendered page): turn 1 named Alice Cohen → Bob Katz, Cc
Carol Mendes, the ask ("review the attached zebrafish report and confirm the
Q1 totals by Thursday") and listed both attachments with the PDF's text;
turn 2 answered "only `Inbox/Quarterly numbers.eml`" citing the PDF line;
turn 3 showed the 3-row table (widget 10, gadget 32, sprocket 58) with the
attachment path `…Quarterly numbers.eml#att:1:data.csv` in the tool card.

Lower layers: `files` holds `Quarterly numbers.eml.txt` (text/plain) and
`Quarterly numbers.eml#att:1:data.csv` (text/csv); 243 `/api/*` responses
observed in the browser, none non-2xx; no traceback in the backend log.

**Observation (model-side, not a defect of this change):** the agent summed
the three amounts in its head and wrote **104** (10+32+58 = 100). The tool
returned the correct rows; `read_file`'s description already tells the model
to go through `create_data` for any sum. Haiku ignored it on this prompt. The
figure a user sees is wrong, so it is recorded here rather than hidden; the
fix belongs to the planner/prompting layer, not to email parsing.

### Round 2 — network_dir agent ("Mail Folder"), five turns

The composer's agent picker is additive, so this report had both agents
attached; the planner queried both connections per turn (the SharePoint calls
mirrored round 1 and are omitted). Mail Folder calls (`tool_executions`):

| Turn | Prompt | Tool calls on `Mail Folder` → result |
|---|---|---|
| 1 | *Who sent the email about quarterly numbers…* | `search_files("quarterly numbers")` → 1 hit (keyword index); `read_file("Inbox/Quarterly numbers.eml")` → text |
| 2 | *Which emails have attachments mentioning pangolin?* | `search_files("pangolin", deep=true)` → 1 hit (index + live scan); `grep_files("pangolin")` → **1 match in 1/7 files** — grep now runs over the email's RENDERED text (headers/body/attachment text) instead of the raw base64 MIME |
| 3 | *Open the CSV attached … total the amount column.* | `read_file("Inbox/Quarterly numbers.eml#att:1:data.csv")` → tabular, 3 rows × 2 cols |
| 4 | *What does the Outlook quote email say, and what was the original request it embeds?* | `search_files`, `grep_files` (no false hits), `read_file("Inbox/Outlook quote.msg")` → text |
| 5 | *מי שלח את הדוח הרבעוני ומה היא ביקשה?* (Hebrew) | answered from context (the email was already read this report) |

Answers: turn 4 reported sender/recipient/date/subject of the **real `.msg`**,
its body ("axolotl project quote … Marmoset pricing"), `invoice.csv` with its
rows, and the **embedded message** ("Original request", Bob → Yael, "please
quote the axolotl project with marmoset pricing") — i.e. a nested Outlook
message surfaced as `Original request.eml` and rendered inline. Turn 5 was
answered in Hebrew with the sender and the ask. `files` holds
`Outlook quote.msg.txt`, `FW Vendor shortlist.eml.txt`, the `.eml.txt` and
the `#att:1:data.csv` derivative.

### Round 3 — chat upload of `Quarterly numbers.eml` through the paperclip

**Pass 1 — defect.** Turn 1 (*Read the attached email: who sent it…*):
`read_file(file_id="<upload uuid>")` **failed** with
`Directory path points at a protected system or application directory`. The
model then fell back to the Mail Folder connection and answered from there
(correctly, but not from the upload). `files` shows the upload row
(`Quarterly numbers.eml`, `message/rfc822`) was created fine — the READ was
refused.

Root cause (pre-existing on `main`, introduced by the security-hardening
bundle #1304): `NetworkDirClient.__init__` and `_root()` run
`check_network_dir_root`, which refuses any root inside the app directory —
and `SessionFileClient` builds a `NetworkDirClient` rooted at
`uploads/files`, which IS inside the backend directory by default (dev and
the Docker image alike). So every `read_file` of a conversation upload — PDF,
CSV, not just email — fails in a default deployment; the unit tests never
saw it because they put session files under `tmp_path`.

Fix: `NetworkDirClient(trusted_root=True)` skips the admin-facing root policy
(every per-file confinement check stays); `SessionFileClient` and
`EmailAttachmentClient` pass it, because those roots are the app's own
choice, not a form value. Test: `TestChatUpload::test_upload_under_the_app_directory_is_readable`
(creates the session file under `uploads/files/`), shown to fail with the
fix reverted.

**Pass 2 (backend restarted with the fix).** Turn 1:
`read_file(file_id="54f4b693-…")` → `text`, `session_file_id` = the upload's
own id; the answer named Alice → Bob, the ask, both attachments (with the
PDF's text and the CSV rows) — all from the uploaded file. Turn 2 (*Read the
data.csv attachment…*): the model answered from the attachment text already
in its context rather than issuing a read of `<uuid>#att:1:data.csv`; the
session-file attachment-id path is therefore covered by the unit test above
and by round 2's connector reads, not by a live LLM call in this pass.

**Pass 3 — a leading prompt to force an attachment read.** Prompt: *Load the
CSV attached to the uploaded email as a dataset and compute the total of its
amount column with create_data.* The agent read the uploaded email
(`read_file(<uuid>)` → text), then called **`create_data` with
`source_file_ids=["<uuid>#att:1:data.csv"]`** — the attachment id straight
from the email's text — which `create_data` rejected ("source file(s) not
found"), after which it recreated the CSV by hand with `write_csv`. Attachment
ids are a `read_file` concept; the analysis tools take the `session_file_id`
a read returns. Addressed in this PR by stating that explicitly in the
`read_file` description and the `file_id` field; accepting `#att:` ids in
`create_data` / `inspect_data` (materialize-on-resolve) is left as a
follow-up.

## Defects found by the loop and fixed

1. **Chat uploads unreadable in a default deployment** (pre-existing, see
   round 3) — fixed with `trusted_root`, regression test added.
2. **Agent picker is additive** (sandbox-script issue, not product): the
   first network_dir run went entirely to the SharePoint agent because the
   composer button no longer read "Auto"; `02_chat.js` now opens the picker
   whatever label it shows. Recorded so the next loop does not repeat it.

Not defects, recorded for honesty: the Haiku sum of 104 instead of 100 in
round 1 (model arithmetic; the tool data was correct) and the SharePoint
Server connector's lack of `grep_files` (pre-existing capability gap; the
model recovered via `search_files deep=true`).

## What this loop does NOT prove

- Graph (OneDrive/SharePoint Online), S3, Google Drive and Documentum were
  not driven live; they read emails through the same `DOC_EXTS` branch and
  `read_raw_bytes` path (unit-covered via the SharePoint 2010 adapter and
  network_dir), but no live tenant/bucket was available in the sandbox.
- No vision-model escalation for an email (there is nothing to rasterize).
- The `.msg` fixtures are synthesized by this repo's writer; a message with
  RTF-only body (compressed RTF, no PR_BODY) or a signed/encrypted message
  was not exercised — extract-msg handles RTF de-encapsulation, but that path
  is unverified here.
