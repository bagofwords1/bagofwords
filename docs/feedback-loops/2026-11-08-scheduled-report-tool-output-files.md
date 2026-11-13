# Feedback Loop: "1,128 auto-generated files, no delete option, run cost keeps growing"

A customer runs a daily scheduled task on one report that calls two custom API
tools (`get_user_cost_report`, `cost_report`). Every call's response is saved
as a file and attached to the report: 1,128 files in three months. They asked
for three things:

1. **Delete them.** The Files page only offered Download.
2. **Stop them piling up.**
3. **Confirm whether they are loaded into each run**, because run cost kept
   growing with the report's history.

## Root cause (validated)

- **Every successful `execute_mcp` call saves a file.** It is created with
  `source_kind="artifact"` and attached to the report
  (`backend/app/ai/tools/implementations/execute_mcp.py`, `_materialize_to_json`,
  which names the file `{tool}.json`). Nothing turns this off, and that is
  intended: later tools read the result by `file_id`.
- **Every run listed every file on the report.** `FilesContextBuilder.build`
  (`backend/app/ai/context/builders/files_context_builder.py`) turned each
  `report.files` entry into a `<file>` element with no limit on count. Tool
  output got the one-line "index" tier, but one line per file still grows with
  the number of earlier runs. *Validated:* the e2e test below counts the
  `<file id=…>` elements in the real prompt.
- **No way to delete a file.** The only delete route,
  `DELETE /reports/{id}/files/{file_id}`, removes the report link; the file row
  and its bytes stay. There was no `DELETE /files/{id}`.
- **Found along the way:**
  - The Files page Download called `/api/files/${file.path}`, which no route
    serves.
  - Every filename on the Files page had a stray trailing `.`.
  - The scheduled-task view listed every report file by name, so it showed
    hundreds of `cost_report.json`.

## Loop A: deterministic reproduction (no external services)

The LLM is stubbed with a scripted planner. Routes, services, the agent loop
and the custom API client run for real against a local HTTP server, so the
tool output files come from real calls.

```bash
cd backend
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/e2e/rbac/test_file_delete_and_generated_files.py -m e2e --db=sqlite -n 4
uv run pytest tests/unit/test_files_context_tiering.py
```

**Before the fix** (backend changes stashed): `7 failed, 1 passed`.
- The delete tests fail because the routes do not exist.
- The prompt-cap test fails because every earlier run's output is listed.
- The cross-org test passes either way; it only asserts that nothing is deleted.

## The fix

- **Listing cap.**
  - `limit_generated_files` keeps the newest `GENERATED_FILES_MAX` (20)
    agent-produced files in `<files>`.
  - Uploads, files mentioned or attached this turn, and agent or project
    library files are never dropped.
  - A `<note omitted="N">` tells the model how many were left out.
  - Older files stay on the report and can still be read by id.
- **Real delete.**
  - `POST /files/delete` (bulk, reports a result per id) and
    `DELETE /files/{id}`.
  - Allowed for the file's creator or a full admin. Anyone else, or another
    org, gets `file.not_found`.
  - Files backing a dashboard are refused with `file.in_use`.
  - Removes the report, agent and project links, tags, sheet schemas, the
    row, the local file and the object-store copy.
  - Audited as `file.deleted`.
- **Files page.**
  - Search, a source filter (all / uploaded / tool output), a "Tool output"
    badge.
  - Select all shown, bulk delete behind a confirmation, per-row delete.
  - A working download, and localized dates.
  - `GET /files` now returns `can_delete` for each file.
- **Scheduled-task view.** Lists attached files by name and only *counts* tool
  output ("40 files produced by tool runs"), linking to the Files page.

**After the fix:**

```
tests/e2e/rbac/test_file_delete_and_generated_files.py  8 passed
tests/unit/test_files_context_tiering.py                 19 passed
existing file e2e suites (tests/e2e/*file*, rbac/*file*) 128 passed
```

**Live check on a devbox.** I seeded 40 tool-output files and the customer's
template on one scheduled report, then in the Files page searched
`cost_report.json`, selected all and deleted. Result: "40 files deleted"; the
template remains, and the task view now shows only the template. Evidence is in
`media/pr/feat-report-file-hygiene/`, including a Hebrew (RTL) capture.

## What this proves, and regression notes

- **Prompt size is now bounded.** It no longer grows with how many scheduled
  runs a report has had. The tests vary the cap and the number of runs, not
  the reported 1,128.
- **Delete covers both roles and the edge cases:** creator, other member, full
  admin, another org, and bulk with mixed ids.
- **Not changed:** tool output is still saved as a file, because later tools
  read it by id. There is no time-based retention job; deleting old output is
  now possible but stays manual.
- **Customer cleanup.** For their existing 1,128 files, an org admin can now do
  it themselves on the Files page: search `cost_report.json`, select all,
  delete. Repeat for `get_user_cost_report.json`.
