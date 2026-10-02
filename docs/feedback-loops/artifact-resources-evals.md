# Artifact resources — real-world eval plan (PR #1220)

**Status: draft, not yet run.** Seven end-to-end evals for artifact resource apps
(collections, files, AI streams, permissions, publication). Each one is a
`sandbox-feedback-loop` run: boot the full stack, drive the real agent
through the UI with Playwright, and check the result at three layers:
**UI** (what each persona sees), **HTTP** (calls made directly to the API,
bypassing the UI), and **DB** (`artifact_resources` / record rows / artifact versions).

## Shared setup

- Stack: `tools/agent/boot_stack.sh`, `BOW_ARTIFACT_RESOURCES_ENABLED=true`, on both
  SQLite and PostgreSQL. Real LLM key comes from an env var only.
- Seed with `tools/agent/seed_org.py`, plus these personas:

| Persona | Role | Groups |
|---|---|---|
| **Olivia** | org admin, creates the apps | — |
| **Ed** | member | `editors` |
| **Mia** | member | — |
| **Ravi** | member | `approvers` |
| **Anon** | logged-out, public `/r/` link only | — |

- Every eval records: the prompts used, agent tool calls (`create_artifact` args),
  screenshots per step (`ui-evidence`), the HTTP status of each direct call, and DB
  row counts before and after each step.
- **Scoring.** An eval passes only when every check passes. Record the agent's
  attempts separately: an app that works only after 3 "fix it" turns still
  passes, but the turn count goes in the report.

---

## E1 — CRUD app: task tracker (create → use → evolve the schema)

**Covers:** the basic collection lifecycle, unique fields, concurrent edits, and
schema evolution without losing data.

1. Olivia: *"Build a task tracker. Tasks have title, assignee, status
   (todo/doing/done), due date and a unique ticket key. Let me add, edit,
   complete and delete tasks, and filter by status."*
2. Create 25 tasks in the UI, edit 5, delete 3, and filter by each status.
3. Create two tasks with the same ticket key, then edit one task from two browser tabs.
4. Olivia: *"Add a priority field (low/med/high) and sort by it."*

**Pass when:**
- [ ] The definition is a `collection` with `ticket_key` set to `indexed + unique` and `status` as an enum.
- [ ] The UI and the DB agree after each step: 22 live rows.
- [ ] The duplicate ticket key returns **409** and the UI shows a readable error, with no crash.
- [ ] The second tab's stale save is rejected (revision conflict) and not silently overwritten.
- [ ] After step 4, all 22 existing rows survive with their revisions, and `priority` is added through an explicit resource update (not the rebuild-conflict path, and with no data loss).
- [ ] Mia (owner-only default) can see no tasks, by UI or direct HTTP.

## E2 — Full blog system (multi-collection, publication workflow, public reads)

**Covers:** related collections, field-level write rules, row conditions (`equals`),
field-limited public reads, and the publication endpoint.

1. Olivia: *"Build a blog. Posts have title, slug (unique), body, tags and status
   draft/published. Only the `editors` group can write posts and only
   editors can publish. Anyone with the public link can read published posts. Logged-in users
   can comment; people can edit or delete only their own comments. Hide author IDs from
   the public."*
2. Ed writes 3 posts and publishes 2. Mia comments on both published posts, then edits and deletes one of her comments.
3. Olivia publishes the app. Anon opens the `/r/` link.

**Pass when:**
- [ ] The resources are `posts` + `comments`. `status.write` is restricted to `groups:[editors]`, public read uses `equals:{status:"published"}`, and `fields` excludes author/owner fields.
- [ ] Anon sees exactly 2 posts, never the draft, by UI or direct HTTP (including by guessing its id).
- [ ] Anon's public read responses contain no `owner_id` or author fields.
- [ ] Mia's attempt to create a post or set `status=published` returns 403 from the API.
- [ ] Mia's attempt to edit or delete Ed's comment returns 403.
- [ ] Every mutation by Anon is rejected. Public writes are impossible by schema.
- [ ] Unpublishing the app makes the `/r/` link stop serving data.

## E3 — File uploads: document vault + photo gallery

**Covers:** the `files` resource kind, `file` fields that link records to files,
size and quota limits, downloads, deletes, and image rendering under the CSP.

1. Olivia: *"Build a team document vault: upload PDFs, images and CSVs with a
   title and category, preview images inline, download anything, delete my own
   uploads."*
2. Upload a 50 KB PNG, a 3 MB PDF, a CSV, and a file whose name contains unicode and spaces.
3. Upload a file larger than `max_bytes` (ask the agent to set it to 5 MB), then exceed the resource's total byte quota.
4. Delete an uploaded file whose id a record still references.

**Pass when:**
- [ ] Images render inline in the editor, the `/r/` page and fullscreen, using only `data:`/`blob:`/same-origin sources. No CSP violations appear in the console.
- [ ] The downloaded bytes match the uploaded file's sha256, and the content-type and filename are preserved.
- [ ] An oversized upload is rejected before it is fully buffered (check backend memory and logs), and the UI shows a clear error.
- [ ] A record that references a deleted file degrades gracefully (no white screen).
- [ ] Mia cannot download Olivia's files by direct `GET /files/{id}/content` unless the rule allows it.
- [ ] Anon gets a 401 or 403 on upload.

## E4 — AI streaming: support-ticket summarizer

**Covers:** the `ai` resource kind, streaming, cancellation, model pinning, and
stream admission and rate limits (re-checks review finding #3).

1. Olivia: *"Build a support inbox. Each ticket has a body; a 'Summarize &
   suggest reply' button streams an AI summary into the page and saves it on
   the ticket."*
2. Stream 3 summaries. Cancel one mid-stream by closing the tab.
3. Run 5 concurrent streams on one worker (`BOW_ARTIFACT_MAX_STREAMS=4`) while a 6th client keeps reading records.
4. Fire 7 AI calls within one minute as one user.

**Pass when:**
- [ ] Tokens arrive incrementally (the first token renders before the stream completes), and the saved summary equals the streamed text.
- [ ] A cancelled stream is closed server-side: the upstream LLM stream is aborted and no orphan task or slot remains in the backend logs.
- [ ] The 5th concurrent stream gets **429 "busy"**, and the record reads in parallel all return 200.
- [ ] The 7th AI call in a minute is rate-limited and the UI shows it.
- [ ] `model_id` is pinned at creation and survives a rebuild unchanged.
- [ ] The prompt is stored on the server: the AI prompt text does not appear in the artifact's client code.

## E5 — Edit → break → fix → rebuild (agent repair loop)

**Covers:** iterative editing, error surfacing, the agent's repair loop, and rebuilds that
reuse resources (re-checks review findings #4, #5 and #8).

1. Start from the E1 tracker with 22 rows.
2. Olivia: *"Add a Kanban board view with drag-and-drop between statuses."*
3. If it renders cleanly, induce a runtime error: *"Use the `useKanban` hook from
   the SDK"* (which doesn't exist). Then: *"It's broken, fix it."*
4. *"Rebuild the whole app from scratch with a darker theme"*, which triggers a rebuild with `replaces_artifact_id` and the same resources.
5. Ask for a resource with a 63-character name.
6. Delete a resource, then ask the agent to recreate it with the same name.

**Pass when:**
- [ ] The runtime error appears in the UI and is fed back to the agent, and the agent fixes it within 3 turns or fewer.
- [ ] Each rebuild produces a new artifact version with **status completed**. The same resource IDs are reused, and the row count and revisions are unchanged.
- [ ] Old versions still open and read the same live data.
- [ ] The 63-character resource name is created successfully.
- [ ] Recreating a deleted name fails with a clear "name reserved" message, not "different definition". The agent then picks a new name. (The rebuild error message is a known gap.)
- [ ] An implicit schema or permission change during a rebuild gets a CONFLICT, and the agent resolves it with an explicit resource update.

## E6 — Permissions matrix: expense approvals

**Covers:** `groups` audiences, `own` rows, `any_of`, field-limited reads, field
write rules, and view-as mode. Every cell is checked against the API directly, not just the UI.

1. Olivia: *"Build an expense app. Anyone in the org can submit expenses
   and see their own. The `approvers` group sees all expenses and is the only group
   that can set status to approved/rejected. Submitters can edit their own
   expense only while it's pending. Nobody sees other people's bank details
   except approvers."*
2. Mia and Ed each submit 2 expenses. Ravi approves one of Mia's.

**Pass when every cell of this matrix (API status / UI behavior) matches:**

| Action | Mia | Ed | Ravi | Olivia | Anon |
|---|---|---|---|---|---|
| list expenses | own only | own only | all | per rule | 401/403 |
| read others' `bank_details` | ✗ | ✗ | ✓ | per rule | ✗ |
| create | ✓ | ✓ | ✓ | ✓ | ✗ |
| set `status=approved` | 403 | 403 | ✓ | per rule | ✗ |
| edit own expense while pending | ✓ | ✓ | — | — | ✗ |
| edit own expense after approval | 403 | 403 | — | — | ✗ |
| delete others' expense | 403 | 403 | per rule | per rule | ✗ |

- [ ] Each cell is checked twice: once in the UI and once by direct `POST /collections/expenses/records` with that persona's token.
- [ ] In view-as mode, the UI is read-only and no mutation reaches the API.
- [ ] Removing Ravi from `approvers` revokes his access on the next request, with no stale cache.

## E7 — Sharing, deployment and runtime robustness

**Covers:** the public `/r/` page, real deployment topology, insecure origins, theme and
filter messaging, fonts, and legacy artifacts with the flag off (re-checks review
findings #1, #2, #6 and #7, and the fullscreen gap).

1. Olivia: *"Build a public event RSVP page with a live attendee counter and a
   filter by session."* Publish it.
2. Open it as Anon via the **shipped `docker-compose.yaml` + Caddy** setup, from 3
   different client IPs, at ~80 views/min.
3. Open the app over plain `http://<LAN-IP>` (not localhost).
4. Toggle the theme and use filters on `/r/`, in the editor, and in **fullscreen**.
5. Restart with `BOW_ARTIFACT_RESOURCES_ENABLED=false`, then open a pre-existing
   legacy dashboard artifact and the RSVP app.

**Pass when:**
- [ ] Anon gets a separate rate budget per client IP behind Caddy, so no 429 at ~80 views/min across 3 IPs. (Expected to fail until `FORWARDED_ALLOW_IPS` is set in the compose file and Helm values.)
- [ ] A spoofed `X-Forwarded-For` from an untrusted peer does not change the rate key.
- [ ] Over plain HTTP the app renders, and both the iframe nonce and record mutations work.
- [ ] Theme toggles and filter re-runs land in all three views, fullscreen included. (Fullscreen is expected to fail: the known gap.)
- [ ] Vendored fonts load in opaque frames with no CORS errors. API responses carry no `Access-Control-Allow-Origin`.
- [ ] With the flag off, legacy artifacts behave exactly as on `main`: data pushes, filters, theme and HTTPS images all work. The resource app fails with a clear "not enabled" message instead of a blank page.

---

## Coverage map

| Area | E1 | E2 | E3 | E4 | E5 | E6 | E7 |
|---|---|---|---|---|---|---|---|
| Create from prompt | ● | ● | ● | ● | | ● | ● |
| Edit / rebuild / fix loop | ● | | | ● | ● | | |
| Collections CRUD | ● | ● | ● | ● | ● | ● | ● |
| File uploads | | | ● | | | | |
| AI streaming | | | | ● | | | |
| Permissions / RBAC | ○ | ● | ● | | | ● | ○ |
| Public sharing / anonymous | | ● | ○ | | | ○ | ● |
| Deployment / runtime | | | ○ | ● | | | ● |
| Review findings re-checked | | | #7 | #3 | #4 #5 #8 | | #1 #2 #6 #7 |

● primary · ○ secondary
