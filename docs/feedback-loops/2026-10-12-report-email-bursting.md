# Feedback Loop — "Scheduled and shared report emails send the owner's rows to every recipient"

Reported behavior: scheduled-report emails, "share by email" and
notify/send_email attachments are rendered **once**, from the report owner's
stored rows, and that one rendering goes to every recipient. For an
identity-scoped query (a `source: identity` param such as "my accounts", or
any step that consumes one), those rows are the owner's slice. So every
subscriber received the owner's private rows. That included members who
cannot open the report and addresses outside the organization.

Decided design ("bursting", NPrinting-style): render each org member's copy
**as that member**, through the same viewer pipeline the live app uses. Skip
addresses with no account, and members who cannot open the report. Leave
reports with no identity-scoped content exactly as they were.

## Root cause (validated, base `ai/friendly-dijkstra-2e8qmj`)

| Path | Where it renders once with the owner's rows |
|---|---|
| Share by email (`POST /reports/{id}/notify`, `share_dashboard`) | `notification_service.py:416`: `generate_for_report(report_id)` with no viewer. One message goes to every address. |
| Scheduled-prompt results email | `notification_service.py:701`: the same unscoped PDF. The run's written summary (`last_content`, drawn from the owner's rows) goes into the body. The recipients are collected into one list at `:652-656`, with no membership or access check. |
| Scheduled report refresh email | `notification_service.py:623` path with no attachment. It is link-only, but it still goes to every subscriber, external addresses included. |
| notify / send_email attachments | `email_send_service.py:461`: `export_step_to_csv`, the raw `step.data`. `:531`: `generate_for_artifact` with no viewer. |
| Public deck download for a signed-in member | `routes/report.py:869` (#1340). `any_rows_withheld` is False once the member has their own result for the identity step, so the deck baked for the owner was served to them. |

The viewer pipeline already existed and was correct. `ReportService.viewer_rerun_report_steps` (the `/r/{id}/run` path) runs a step **as the viewer** into their own `step_user_results`. Identity params bind to that viewer's email, groups and profile. `viewer_data_policy.resolve_step_data` then serves those rows. The email paths never went through it.

## Deck finding (validated)

A deck **is replayable without an LLM**. `create_artifact` stores the
python-pptx script in `content['code']` and executes it with
`PptxCodeExecutor.execute_pptx_code(code, visualizations, report, images)`
(`create_artifact.py:1436-1450`). `edit_artifact` re-executes that same stored
code with `max_repairs=0`, which means no LLM (`edit_artifact.py:220-245`). Only
the rows change between viewers. So a member's deck is the stored code
re-executed in the sandbox over that member's rows
(`ReportPdfService.deck_bytes_for_viewer` / `_replay_deck`).

Withholding remains for the case with no stored code. That covers a legacy deck
that exists only as baked slide JSON (`content['slides']`), or only as a binary.
Those decks are not sent to non-owners. Live check: Alice's and Bob's deck PDFs
contain only their own accounts (see Loop B).

## Loop A — deterministic (pytest; SMTP client and Chromium/LibreOffice stubbed)

`backend/tests/e2e/rbac/test_report_email_bursting.py`. The SMTP stub is
`settings.email_client`, and it records every message at send time. The
renderer stub writes the rows it was handed into the "PDF". The step code,
the viewer runs, the policy and the deck executor all run for real.

```bash
cd backend && ENVIRONMENT=production TESTING=true .venv/bin/python -m pytest -q --db=sqlite \
  --disable-warnings tests/e2e/rbac/test_report_email_bursting.py
```

| Test | Base | Fix |
|---|---|---|
| `test_scheduled_results_give_each_member_their_own_rows` | FAIL (one message to all 5, owner rows) | pass |
| `test_scheduled_results_keep_going_when_one_recipient_fails` | FAIL | pass |
| `test_share_email_of_an_identity_dashboard_is_personal_per_member` | FAIL (external got the owner's rows) | pass |
| `test_share_email_to_only_skipped_recipients_reports_an_error` | FAIL (sent to external) | pass |
| `test_identity_deck_is_rerun_from_its_stored_code_per_member` | FAIL (owner deck to all) | pass |
| `test_public_deck_download_for_a_member_with_own_rows_is_not_the_owners` | FAIL (`OWNER-ACCT` in A's deck) | pass |
| `test_notify_attachment_of_identity_rows_is_rendered_per_recipient` | FAIL (owner rows in B's CSV) | pass |
| `test_link_only_report_run_email_skips_non_members_without_running_queries` | FAIL (external + no-access member mailed) | pass |
| `test_scheduled_results_for_a_plain_report_are_unchanged` (guard) | pass | pass |
| `test_share_email_of_a_plain_dashboard_is_unchanged` (guard) | pass | pass |

Base: **8 failed, 2 passed**. Fix: **10 passed**.

## Loop B — live stack (real Chromium PDFs, real LibreOffice, real SMTP)

`report-email-bursting/run_loop.sh <backend_dir> <work_dir> <api_port> <smtp_port>`
sets up and runs the loop:

- It starts a backend on a private port (8061 for before, 8062 for after) with a scratch SQLite DB.
- The SMTP config points at `smtp_sink.py`, an aiosmtpd sink that writes each message to `.eml`.
- `tools/agent/seed_org.py` creates the owner, `admin@`, and invites the members `alice@`, `bob@` and `carol@`.
- `seed_bursting.py` seeds three reports:
  - an **identity dashboard**: the step filters a book of accounts by the running user, and the snapshot is `OWNER-ACCT-*`;
  - a **plain dashboard** (`PLAIN-ROW-*`);
  - an **identity deck**: stored slide code, plus the `.pptx` baked for the owner.
- `trigger.py` shares each report with Alice and Bob. Carol is a member with no access, and `partner@acme-partner.com` has no account. It then fires three paths:
  1. share by email over HTTP;
  2. the scheduled-prompt results email, in process;
  3. the scheduler's job body `scheduled_rerun_report_steps` after `POST /schedule`.
- `read_mail.py` extracts each attachment's text with pypdfium2 and counts the row labels.

```bash
# from the repo root; the backend dir needs a .venv and frontend/public/libs vendored
bash docs/feedback-loops/report-email-bursting/run_loop.sh backend /tmp/bursting-after 8062 2532
```

"Before" ran a `git archive` of the base branch, and "after" ran this branch.
Full output: `report-email-bursting/mail-before.txt` and `mail-after.txt`.

| Email / recipient | Before | After |
|---|---|---|
| Identity dashboard, share by email: alice / bob | **OWNER-ACCT x3** (one message to all 4) | ALICE-ACCT x2 / BOB-ACCT x1 (one message each) |
| … carol (member, not shared) / partner (external) | **OWNER-ACCT x3** | not sent (`skipped` in the API response) |
| Identity dashboard, scheduled-prompt results: owner | OWNER-ACCT x3 + owner summary | OWNER-ACCT x3 + owner summary (same 40063-byte PDF) |
| … alice / bob | **OWNER-ACCT x3 + owner summary** | own rows, no owner summary |
| … carol / partner | **OWNER-ACCT x3 + owner summary** | not sent |
| Identity deck (PDF of the .pptx), share and scheduled: alice / bob | **OWNER-ACCT x3** | ALICE-ACCT x2 / BOB-ACCT x1 (stored code re-run) |
| Scheduled report refresh (link only), identity reports | to all 5 incl. carol and partner | owner, alice, bob (one each); carol and partner skipped |
| Plain dashboard, all three paths | one message to all recipients, PLAIN-ROW x4 | **identical**: one message to all, PLAIN-ROW x4, same 36667-byte PDF |

The owner gets one in-app `delivery_skipped` notification (warning) per
send, and it names who was skipped. For example: "2 recipient(s) were not
sent this report … partner@acme-partner.com, carol@example.com". No
per-recipient PDF was left under `uploads/pdfs` (0 `burst-*` files).

## The fix

- **`services/report_bursting.py`** (new):
  - `report_identity_scoped` decides whether a report needs bursting: does any query render an identity-scoped step (`identity_taint.step_identity_scope`)?
  - `classify_recipients` sorts recipients into the owner, active org members and everyone else.
  - `prepare_recipient_copy` → `ReportService.viewer_rerun_report_steps(..., only_identity_scoped=True)`. This is the live app's share gate, agent access, credentials and identity binding. Only identity-scoped steps run, because the other steps serve the shared snapshot exactly as in the app. A link-only copy, with nothing rendered, applies the share gate alone and runs no queries.
  - `deliver_burst` handles recipients in order:
    - the owner gets the unscoped render, the same as before;
    - external addresses are skipped;
    - members are processed sequentially. Each one is rendered with `generate_for_report(viewer=member, out_key=burst-<uuid>)`, sent alone and deleted.
  - Limits: `BURST_MAX_RECIPIENTS=50` and `BURST_RECIPIENT_TIMEOUT_S=300`. One recipient's failure never stops the next.
  - `notify_owner_of_undelivered` sends the owner an in-app summary of who was skipped.
- **`notification_service.py`**:
  - The share-dashboard and scheduled-results paths call bursting only when the report is identity-scoped. Otherwise the original code runs unchanged.
  - Personal copies of scheduled results drop the run's written summary, which is the owner's narrative.
  - `NotifyResponse.skipped` is a new field. When *nothing* was sent, the skips are also reported in `errors`, so the dialog never says "sent".
- **`report_pdf_service.py`**:
  - `generate_for_report` and `generate_for_artifact` take `viewer=` and `out_key=`. A per-recipient copy gets its own file name, so two readers' PDFs can never overwrite or be served as each other.
  - For decks, `deck_bytes_for_viewer` returns one of three things:
    - the baked deck, when the reader's rows are the stored rows;
    - a replay of the stored code, when the reader has their own rows;
    - nothing, when rows are withheld or there is no code.
- **`artifact_payload.viewer_rows_state`** classifies what a reader gets: snapshot, personal or withheld.
- **`routes/report.py`** (public `export_pptx`): a signed-in member who has their own rows gets the deck replayed for them, not the owner's file. Withheld rows still return 409, as in #1340.
- **`email_send_service.py` / `notify_service.py` / `send_email` tool**: attachments are rendered for the recipient:
  - identity-scoped CSV, XLSX and PDF attachments use that recipient's rows;
  - a recipient with no account or no access gets the message without the attachment;
  - the owner gets the snapshot, as before.
- **`locales/*.json`**: `notifications.content.delivery_skipped.{title,body}` in all 10 catalogs.

## What this proves / regression notes

- Each member receives only their own identity-scoped rows on every email path. Nobody but the owner receives an owner row, and external addresses and no-access members receive nothing.
- Plain reports produce byte-identical attachments and the same single message to the same recipients, external addresses included. No viewer runs happen for them (pinned by test).
- Two existing mock-DB unit tests needed one adjustment each:
  - `test_notification_service.py` now patches `report_identity_scoped` to False to state its "plain report" precondition.
  - `notify_service` now looks up the recipient user only when there are attachments, which also keeps its existing mocks valid.
- Behavior changes, intended, and only for identity-scoped reports:
  - external addresses no longer receive these emails;
  - members who cannot open the report are skipped, including report-per-run scheduled prompts whose spawned report is private to its owner;
  - personal copies carry no owner-written run summary.
- Sandbox notes:
  - The loop used ports 8061/8062 and 2531/2532, with scratch DBs under a private scratchpad folder.
  - The vendored JS libs were copied into the worktree's `frontend/public/libs`, which is gitignored.
