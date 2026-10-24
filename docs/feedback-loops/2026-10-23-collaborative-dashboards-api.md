# Collaborative dashboards — API reference for the UI

Companion to `2026-10-23-collaborative-dashboards.md`. Backend is in
`backend/app/routes/dashboard.py`, `backend/app/routes/dashboard_collab.py`,
services `dashboard_collab.py`, `dashboard_presence.py`,
`dashboard_access.py` (`capabilities`). All paths below are under `/api`
(frontend: `useMyFetch('/…')`, which prefixes `/api`).

## Capabilities

`GET /d/{ref}` and `GET /d/{ref}/draft` return `capabilities: string[]`, a
subset of:

| cap | meaning |
|---|---|
| `view` | open the published dashboard |
| `edit` | open the draft; "Edit in my conversation" |
| `publish` | publish a version; Roll back; Refresh |
| `share` | change viewers/editors (PUT sharing) |
| `make_public` | set visibility `public` (owner only) |
| `settings` | schedule, chat, Data tab, link name, notes |
| `run_identity` | switch viewer↔creator credentials (owner only) |
| `transfer` | hand the dashboard to another editor (owner, org admin) |
| `analytics` | read view analytics |

Show a button only when its capability is present. An editor of a
dashboard set to run as its creator has `edit` but not `publish`; a publish
attempt then fails with `error_code: dashboard.creator_publish_owner_only`.

## Draft page meta — `GET /d/{ref}/draft[?version=N][&draft=slug]`

```json
{ "id", "slug", "title", "mode", "report_id", "version_id", "published",
  "theme_name", "theme_overrides", "general", "can_edit", "is_owner",
  "capabilities": [...],
  "version_number": 12,                 // the version shown
  "version_author": {"id","name"},      // whose conversation it was made in
  "version_is_mine": false,             // true: its rows are the reader's own
  "my_report_id": "uuid|null",          // the reader's own editing conversation
  "draft": {"id"|null, "name", "slug", "status", "head_version_id", "head_version_number", "updated_at", "created_by"},
  "drafts": [ same shape; first is {"id": null, "name": "Main", "slug": "main"} ] }
```

`version` (a number) previews any version of a live editing conversation;
`draft` (slug) shows that named draft's newest. The other draft routes take
the same two query params: `/d/{ref}/draft/content`, `/draft/queries`,
`/draft/queries/{qid}/step`.

A step that is still re-running for its new editor comes back as
`{..., "data": {}, "pending": true}` — show "Running as you…" on that chart.

### Someone else's version
`version_is_mine: false` → the chart data may be withheld (`snapshot_withheld:
true`, per-user / RLS sources) or be the shared snapshot. Offer **Run as me**:
`POST /d/{ref}/draft/run?version=N` → `{steps_total, steps_succeeded, steps_failed, ...}`;
then reload steps (they now carry `viewer_result`, the reader's own rows).
For the reader's own version it returns `{"own": true}` (nothing to do).

### Who changed what — `GET /d/{ref}/draft/changes[?version=N][&draft=slug]`
```json
{ "version_id", "live_version_id",
  "elements": [{"key", "title", "changed_by": {"id","name"}, "changed_at", "version_number",
                "vs_live": "added"|"changed"|"unchanged"}],
  "removed_since_live": [{"key","title","vs_live":"removed"}],
  "published_since": {"by": {"id","name"}, "at", "release_id"} | null }
```
`key` is a chart's element key; a chart's visualization row exposes it as
its id unless `element_key` is set (copies keep their source's key).

## Edit / join — `POST /dashboards/{id}/edit` body `{"draft": "slug"}` (optional)
→ `{"report_id", "created": bool, "rerunning": bool}`. Navigate to
`/reports/{report_id}`. `rerunning: true`: the charts are running again with
the caller's credentials (copies of someone else's work). Errors:
`dashboard.join_no_access` (params `name`: the data source they lack),
`dashboard.edit_first` (nothing to edit).

## Publish — `POST /dashboards/{id}/publish` body `{"version_id"?, "force"?}`
202 + status. **409 `dashboard.published_since`** with `params: {by, at}`:
someone published since this draft started. Show "{by} published {at
relative}. Review changes / Publish anyway" — "Publish anyway" re-POSTs with
`force: true`; "Review changes" opens the changes view.
`GET /dashboards/{id}/publish` → `{published, latest, has_unpublished_changes, last_run}`.

## Sharing — `GET|PUT /dashboards/{id}/sharing`
```json
GET → { "visibility": "none|shared|internal|public", "published",
        "user_ids", "group_ids",                 // viewers (count while 'shared')
        "editor_user_ids", "editor_group_ids",   // editors (any visibility, before publish too)
        "owner_id", "project_id" }
PUT ← { "visibility", "user_ids"?, "group_ids"?, "editor_user_ids"?, "editor_group_ids"? }
```
A list left out stays as it is. Someone in both lists is an editor. Errors:
`dashboard.share_requires_publish` (visibility ≠ none before the first
publish — editors can still be added with visibility `none`),
`dashboard.public_owner_only`. GET needs `edit`; PUT needs `share`.
Everyone who can open the dashboard's project edits it (`project_id` set →
show "Everyone in project <name> can edit").

## Settings — `GET|PUT /dashboards/{id}/settings`
Adds `notes` (string|null; PUT `""` clears). PUT needs `settings`;
`shared_run_identity` changes need `run_identity`.

## Drafts
- `GET /dashboards/{id}/drafts` → `[DraftSchema]`, first is Main (implicit).
  Show a draft picker only when there are 2+.
- `POST /dashboards/{id}/drafts` body `{"name", "from_version_id"?}` → join
  response (a new conversation on that draft). 409 `dashboard.draft_name_taken`.

## History — `GET /dashboards/{id}/history`
```json
{ "conversations": [{"report_id","user":{id,name},"started_at","ended_at","versions","draft","can_open"}],
  "releases": [{"id","status","live","version_id","version_number","published_by":{id,name},"published_at","error"}],
  "versions": [{"id","number","author":{id,name},"created_at","report_id","draft","published"}],
  "drafts": [DraftSchema] }
```
Link a conversation only when `can_open`. Each ready, non-live release gets
**Roll back**: `POST /dashboards/{id}/rollback {"release_id"}` (needs
`publish`) → publish status. 404 `dashboard.release_not_kept`.

## Transfer — `POST /dashboards/{id}/transfer {"user_id"}` (needs `transfer`)
→ sharing (the previous owner becomes an editor). 400
`dashboard.transfer_not_editor` when the target cannot edit.

## Presence — `/dashboards/{id}/presence`
- `PUT {"report_id"?}` every ~15 s while an editor has the dashboard open
  (report_id: the conversation they're in, when in one) → `{"entries": [...]}`.
- `GET` → same. `DELETE ?report_id=` on leave (use `navigator.sendBeacon`-
  like best effort / `onBeforeUnmount`).
- entry: `{"user":{id,name},"report_id","state":"viewing"|"agent_editing","element_keys":[...],"seen_at","is_me"}`.
  Show others (not `is_me`) as avatars: "Avi is also editing"; when
  `state == agent_editing`, "Avi's agent is editing" and outline the charts
  whose key is in `element_keys`.

## Conversation card — `GET /dashboards/conversations/{report_id}`
Only for the conversation's own user; else `[]`.
```json
[{ "artifact_id", "title", "slug", "continued_from": {id,name}|null,
   "continued_from_report_id"|null, "can_open_previous", "started_at",
   "base_version_number", "summary": {"charts":[{title,query,filters}], "sources":[...],
   "history":[{"version","by","at","added","changed","removed"}], "notes"}, "rerunning" }]
```
Card: "Continued from Dana's conversation · version 3". When `rerunning`:
"Charts are running again with your credentials…".

## Delete conversation preview — `GET /reports/{id}/delete_preview`
Dashboards gain outcome `others_editing`: "Other conversations keep editing
<title>; it stays as it is."

## Dashboards list — `GET /dashboards?filter=…`
Cards gain `role`: `owner|edit|view`. `report_id` is the reader's own
editing conversation (null if they have none): for `role: edit` with no
`report_id`, the pencil calls `POST /dashboards/{id}/edit` then navigates.

## Analytics
`GET /artifacts/{id}/runtime/analytics?days=` is open to editors
(`analytics` capability). Editors' own visits are not counted.
