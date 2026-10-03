---
key: data-app-design
title: Data apps
description: Use when building or editing apps, dashboards, blogs, forms, or tools with saved records, uploads, AI streaming, permissions, or analytical data.
category: dashboard
version: "1.1"
order: 25
default_enabled: true
modes: [chat, training]
tags: [data-app, dashboard, design, records, files, permissions]
---

Design the working interface around the user's task: exploring records,
collecting input, managing content, working with files, comparing results, or
monitoring performance. A dashboard is one view.
Start with a useful working surface and a stable app title. Summary metrics,
hero charts, cards, tabs, and themes are optional; do not add them to fill space.

Use a coherent type scale, readable numerals, aligned controls, useful density,
intentional whitespace, and consistent chart colors. Custom React/CSS and the
built-in kit are both supported. Respect the organization's actual brand.

Connect backend query controls through useParams and useParamOptions. Separate
query parameters from snapshot filtering and local selection/navigation state.
Show loading, errors, empty results, and valid selected states. Preserve valid
selection across query updates and explain when it leaves the result.

Every analytical metric/chart/table needs source provenance; derived values
need honest units, grain, and coverage. Never invent comparisons or totals from partial data.
Only expose actions with implemented behavior and available backend support.

For monitoring requests, use meaningful summary metrics, relevant comparisons,
and compact status/trend views. For catalogs, let search and records lead. For
analysis, give controls and the main chart sufficient space. Do not impose one
composition on every task.

On ordinary edits, preserve the existing app's design, records, and runtime contract.
Organization administrators may customize these conventions; preserve their edits.

## Choose the data contract

Infer persistence from the task: saving drafts, keeping submissions after reload,
or sharing team content requires backend resources even if the user never says
"database". Choose a useful minimal schema; ask only about ambiguity that changes
access, data loss, or the core workflow. Do not require a technical build brief.

Use existing analytical queries and visualization helpers for connected-source
analysis. Use artifact collections for app-owned records, file resources for
uploads, and approved AI operations for model calls. An app may combine them;
it may also have no visualizations. Do not turn analytical datasets into writable
collections or imply that saving an app record updates an external data source.
Use the supplied ARTIFACT RESOURCE SDK reference for exact APIs and limits;
this skill describes the workflow rather than a second API specification.

## Create and evolve resources

For a new app, declare the needed resource definitions in create_artifact along
with the working interface. For an existing app, read_artifact first to inspect
current code, definitions, stable resource_artifact_id, and revisions. Use
manage_artifact_resources to create, update, or delete definitions, then
edit_artifact when the interface needs to change. This tool configures schemas,
permissions, file resources, and AI operations; it does not edit user records.
Generated app event handlers use the SDK for record and file operations.

Reuse the returned stable identity across UI versions. Inspect current schemas
before changing fields, indexes, defaults, or permissions; supply the current
revision for changes that require it. Preserve existing data and unrelated
configuration. Do not delete/recreate a collection to bypass a rejected change.
On conflict, re-read and reconcile rather than retrying stale definitions.
Treat a failed or uncommitted observation as failure, even if the tool completed.
If resources are disabled, explain the organization setting; do not silently
replace persistence with browser storage or claim unsaved work was saved.

## Identity and access

Reuse platform identities and existing groups. Default to private access and
choose the narrowest resource permissions that implement the requested workflow.
Do not invent group IDs, authentication systems, or roles. Keep permissions on
resource definitions, including ownership, row conditions, and field rules when
needed. Validate what the current backend supports before promising access.

Report/artifact sharing controls who can open the app; resource permissions
control what they can read or change. Sharing a link does not grant write access.
Public resource access is read-only. An editable external workflow needs a
supported authenticated identity and explicit permission; do not promise
anonymous submissions. UI capability checks help present controls, but the
backend must authorize every operation. Handle signed-out and forbidden states.
Use existing sharing; there is no separate artifact publication tool or step.

## Build working interactions

Implement actual handlers with empty, loading, success, and error states. Keep
input on failed saves and conflicts; use the SDK's revision and idempotency
contracts. Upload through the file SDK, then retain returned file IDs where the
schema allows. Make progress, cancellation, and failed uploads understandable.
Treat record and file contents as untrusted data, never executable code or HTML.

Start an AI stream only after a user action, display incremental output, and
support cancellation and interruption. Save completed output only when the task
calls for it; do not present partial output as a completed result. Do not promise
background jobs or recovery after reload. Protect secrets through the backend;
never embed credentials in generated code. Keep paid-call verification explicit.
The read-only Data explorer helps inspect stored records; it is not an editing UI.

## Verify the user's journey

Adapt checks to the app, rather than forcing a fixed example or page layout:
- A team updates site: create a post, reload, edit it, and confirm the saved
  content remains after a UI-only change. Check reader versus author access.
- A document helper: upload a supported file, start and cancel a stream, retry
  deliberately, and save a completed result. Handle upload or AI failure.
- A shared catalog: verify the actual sharing mode and allowed records for each
  intended viewer; check that denied writes remain denied outside the UI.
- An analytical app: confirm existing query controls, visualizations, and legacy
  artifacts still work without requiring resources.

Preview uses synthetic capabilities and cannot prove persistence or permission
isolation. Distinguish preview checks from authenticated runtime checks; report
what was actually verified and what remains untested. Never manufacture live
rows or claim a successful save, model call, or access check without evidence.
