# Feedback Loop — any org member could read, list and re-attach every file in the org

A customer asked whether their automation, running with a non-admin personal
API key, could upload files to a report or project and reuse the `file_id`.
It can. But answering that showed that any org member could reach **every**
file in the organization through the files API, including files in other
users' private reports and in private agents' libraries. They could also
detach files from other users' reports and push files into those reports and
agent libraries.

Access rule implemented (`backend/app/services/file_access_service.py`). A
non-admin can see a file when any of these is true:

1. they uploaded it;
2. it is in the library of an agent they can access (a grant, a membership, or a public agent);
3. it is a default file of a project they can view;
4. it is attached to a report whose **conversation** they can view (they own
   it, they collaborate on its project, or the conversation is shared with them);
5. it is **embedded in an artifact** of a report whose conversation *or*
   artifact they can view, **and the report's owner can see the file by rules
   1–4**. A dashboard shared org-wide shows its embedded images, but not every
   file uploaded in the chat behind it. An embed passes on access the owner
   already has; it can't grant access the owner doesn't have.

Full admins see every file. Changing a report's files (upload with `report_id`,
detach) is limited to the report owner, the same rule as every other report
mutation. Uploading into an agent library needs `manage` on that agent.

## Root cause (validated)

- `manage_files` is a **baseline** permission (`BASELINE_PERMISSIONS`,
  `app/core/permissions_registry.py`), granted to every member. Every file
  route was gated only on it plus an org check:
  - `GET /api/files` → `FileService.get_files` filtered on `organization_id` only.
  - `GET /api/files/{id}/content` and `/embed_token` looked the file up by org only.
  - `GET` and `DELETE /api/reports/{id}/files[/{fid}]` used
    `@requires_permission('manage_files', model=Report)` without
    `owner_only`, so the decorator only checked that the report was in the org.
  - `POST /api/files` never checked `report_id` (it even accepted one that
    doesn't exist) or `data_source_id`. Bob could upload into a private agent's library.
- Three attach paths turn a readable-by-id file into one that is readable through the target:
  - `ReportService.create_report` (`files: [...]`) selected files by id with **no org filter**;
  - `MentionService.create_completion_mentions` linked any mentioned file id to the report.
    The live loop showed alice's file contents inside bob's LLM prompt;
  - `ProjectService.set_default_files` accepted any org file id.

## Loop A — curl only, personal `bow_` API keys (`file-access-scope/`)

```bash
cd backend
BOW_DATABASE_URL=sqlite:///db/fileloop.db uv run alembic upgrade head
BOW_ENCRYPTION_KEY=<fernet> BOW_DATABASE_URL=sqlite:///db/fileloop.db uv run python main.py > backend.log 2>&1 &
DB=db/fileloop.db bash ../docs/feedback-loops/file-access-scope/seed.sh state/   # admin + alice/bob/carol + keys (+ LLM if ANTHROPIC_KEY)
bash ../docs/feedback-loops/file-access-scope/verify_files.sh state/
LOG=backend.log bash ../docs/feedback-loops/file-access-scope/verify_agent.sh state/
```

`verify_files.sh` has 58 checks:
- a non-admin creates a report, uploads, lists, downloads (bytes round-trip), mints an embed token, attaches an existing file on report creation, detaches it, and sets a project default;
- a second member can't list, download, embed, detach, upload into, or re-attach that file through report creation or project defaults;
- conversation sharing grants read but not write, and unsharing revokes it;
- an org-internal dashboard doesn't expose chat uploads;
- org-wide projects expose their default files;
- agent files follow agent membership;
- admin sees everything.

`verify_agent.sh` has 11 checks. A non-admin uploads a CSV and asks the agent
about it, @-mentions their own unattached file, and a second member forges a
mention of the first member's file id. The sandbox LLM key had no credit, so
the script asserts on the **outbound LLM request bodies** that the backend
logs, which is exactly what the agent sees.

| run | verify_files | verify_agent |
|---|---|---|
| before (HEAD) | 37 pass / **21 fail** | 8 pass / **3 fail**: alice's data present in bob's prompt |
| after, same DB | 58 / 58 | 11 / 11 |
| after, fresh DB via `seed.sh` | 58 / 58 | 11 / 11 |

## Loop B — pytest (`backend/tests/e2e/rbac/test_file_access.py`)

10 tests, written as invariants over both roles. They fail on HEAD (8 of 10;
the 2 positive-path tests pass on both) and pass with the fix on
sqlite and on Postgres 16 (`--db=external`). Regression run:
`tests/e2e/rbac/` in full, plus the files, mentions, projects, report-sharing,
public-route, doc-artifact and legacy-attach suites (481 + 142 tests, all
green). `test_connection_file_browse.py::test_contract_sharepoint_server`
fails identically on HEAD and is unrelated.

## Round 2 — agent tools

A file id that reaches a tool comes from the model, so tools now apply the same
rule to the **run's principal**: the run's user, or the report owner for runs
without one (schedules, inbound email, notifications). This is
`run_viewable_file_ids` in `file_access_service.py`. It covers:

- `create_artifact` / `edit_artifact_legacy` `file_ids`: other ids are dropped with a warning;
- `create_doc` / `edit_doc` `{{file:<id>}}` placeholders: other ids are dropped from `file_ids`;
- `write_file` `source_file_id` (copying out to a connection): the call returns "not found";
- email/notify `file` attachments (`EmailSendService.resolve_attachment`, which takes the sender): the attachment fails.

**Hole found by the new tests.** Rule 5 first matched the file id *anywhere* in
the artifact JSON, so a doc whose markdown merely mentioned an id granted access
to that file. And `PATCH`/`POST /api/artifacts` accept arbitrary content, so
filtering in the tools alone can't make embeds trustworthy. Rule 5 now reads
only the structured embed lists (`files[].id`, `file_ids`) and requires the
report owner to reach the file by rules 1–4 (`test_embedding_a_file_id_does_not_grant_access_to_it`).

The 5 new tests fail on the round-1 commit and pass now.

## Not changed / residual

- **`POST /api/artifacts`** doesn't check that the caller owns the target
  `report_id`, so any member can add content to another user's report. It no
  longer leaks files (see rule 5), but it's filed as a separate follow-up.
- **`GET /api/files`** lists rules 1–3 only (own, agent and project files). Files
  reached through someone else's shared report are read through that report
  (`GET /reports/{id}/files`), not through the org-wide picker.
- **Admins can no longer detach files from other users' reports.** That now matches
  every other report mutation, which was already owner-only.
