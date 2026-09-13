# Manage connection and personal schema progress

The user requested one shared management screen with readable saved settings,
explicit editing, discovery progress, and scheduling. ConnectionDetail should
focus on the current user's query identity and personal schema.

## Previous behavior and cause

`EditConnectionModal.vue` opened directly into editable settings, while
`ConnectionDetailModal.vue` owned shared refresh, scheduling, and personal
refresh. Personal refresh awaited the synchronous `/my-schema/refresh` endpoint,
which returned a count without a tracked job. Its spinner consequently had no
personal discovery events to display. Prior-layout screenshots are preserved as
`media/pr/manage-connection/before-manage.png` and `before-detail.png`.

## Changes

- Manage connection keeps the original form visible with disabled controls on a white pane. Edit
  settings unlocks the form; Cancel remounts the saved values. Save changes
  validates before updating and returns to disabled controls without closing.
- Non-secret credential identifiers remain visible; saved secret placeholders stay masked.
- The right pane owns test results, shared refresh/logs, scheduling, a collapsed
  rate-limit section, and deletion. Scheduling also appears after successful Add. Auto-reindex is collapsed by default and expands without changing its saved configuration.
- ConnectionDetail retains personal query identity, sign-in/disconnect, and
  personal refresh. Its Manage action follows the `manage_connection` permission.
- Personal refresh opts into `background=true`, returning a tracked user job.
  Polling explicitly requests `scope=user`; shared management requests `scope=org`.
  The default synchronous endpoint response remains compatible with other callers.
- Specialized integration/tool editors retain their existing editor and deletion
  entry points. No connector emission changes belong to this UI work.

## Reproduce and verify

With the Nuxt development server running on port 3100:

```sh
cd frontend
node tests/data_sources/manage-connection-flow.mjs
node tests/data_sources/connection-setup-flow.mjs
```

These use actual Vue components, real connector field schemas, synthetic API
responses, and synthetic permissions. The harness removes its temporary preview
route after execution. Run the scripts sequentially.

Observed PASS: read-only/edit/cancel/save, preserved credentials, shared progress,
schedule persistence, tracked personal progress, Hebrew RTL and mobile. The Add
regression also passes in ten locales and dark mode, including connection failure,
discovery retry without duplicate creation, and completion. Videos and screenshots
are under `media/pr/manage-connection/` and `media/pr/connection-setup/`.

Backend, from the configured Python 3.12 environment:

```sh
cd backend
TESTING=true .venv/bin/python -m pytest tests/e2e/test_connection_indexing_user_scope.py --db=sqlite -q --tb=short --disable-warnings
```

The three existing scope tests passed. The new background-refresh test initially
failed because its fixture was treated as one row instead of a list; after fixing
the fixture access, it passed separately (1 passed, 3 deselected). It verifies that
the returned job belongs to the current user, shared indexing stays separate,
and the legacy synchronous response still works.

Changed Vue components compile, `git diff --check` passes, and the four new labels
exist in all ten catalogs. Existing catalog key drift is unchanged. Browser
coverage uses mocked services; it does not claim live Power BI/Snowflake access or
a final production build. Earlier Edit/Detail screenshot harnesses document the
superseded layout; use `manage-connection-flow.mjs` for the current contract.

September 12 refinement: disabled form fields replace the separate summary; the schedule starts collapsed. Before evidence: `media/pr/manage-connection/before-disabled.png`. The browser flow checks that fields unlock, cancel restores values, saving disables them again, and scheduling still persists after expanding.

Status-pane refinement: consistent 14px results/actions and 12px supporting text; neutral success text with colored icons; shared test/refresh action row; collapsed schedule and rate-limit summaries; compact delete action. Discovery warning/error events remain visible above collapsed logs (latest three distinct messages, with all events retained in logs). Background schedule errors also remain outside the collapsed section. The browser regression passes with a warning fixture and verifies it is visible before opening logs. Before: `media/pr/manage-connection/before-clean-status.png`; after: `overview.png`, `he.png`, `mobile.png`, and recorded video in the same directory.

Diagnostic refinement: the compact failed state shows one generic localized failure, with the original error retained in collapsed Details and logs even when no events exist. Duplicate warning/error wrappers are compared without modifying the original text. Matching schedule errors are suppressed; distinct schedule failures remain visible. Supporting typography uses 12px and results/actions 13px. Last successful test is explicitly labeled separately. The browser regression exercises a repeated HTTPSConnectionPool failure: no raw diagnostic in the collapsed pane, one diagnostic after expansion, and no repeated schedule error. Passed, including existing management flows and RTL/mobile. Evidence: `failure-collapsed.png`, `failure-details.png`, and `before-diagnostics.png`. Generic fallback intentionally avoids guessing the meaning of arbitrary connector exceptions.

SSAS test correctness: `XmlaClient.test_connection` previously relied on
`_list_catalogs`, which returns the configured catalog without network I/O.
The new regression varies catalog scope and timeout, connection, and HTTP 401
failures at the requests boundary. Before the fix: 6 failed, 3 passed (every
scoped case falsely reported success). Scoped health checks now request cube
metadata from the configured catalog; unscoped catalog discovery is unchanged.
This uses the shared XMLA client, so SAP XMLA receives the same correction.

Run from backend:

```sh
TESTING=true .venv/bin/python -m pytest tests/unit/test_analysis_services_client.py tests/unit/test_sap_bw_xmla_client.py -q --tb=short --disable-warnings
```

The typography scale is now defined once on the management status pane:
13px results/actions, 12px supporting text, and a 14px heading, with consistent
line height. Removed the progress component's local font-size overrides.
The browser management regression passed, including the error and RTL/mobile
states. Tests stub remote transport; they do not contact the customer's SSAS host.

Testing indicator now uses the shared Spinner.vue. The browser harness delays its test response to assert the spinner is visible and capture `testing-spinner.png`; the prior user-supplied indicator is saved as `before-testing-spinner.png`.

ConnectionDetail now uses a compact access dialog: connector title and status,
one count for the selected access scope, account controls in one subdued section,
and personal refresh/sign-out. Details and the authorized Manage action share a
footer. Consumers have no manager controls; shared connection failures give them
an administrator-directed next step. Technical connector IDs are omitted.

ConnectionDetail now uses getEffectiveStatus rather than its previous default-to-
healthy boolean. The resolver handles not_connected/error/offline and direct
cached test fields; missing status (including credentials without a test) remains
unknown. Manage emits a completion notification after tests, and Detail and
AgentConnections refresh their backing data for successes and failures.

The browser flow covers admin/member visibility, personal counts, refresh progress,
failed shared connection status, and the compact warning diagnostics. Visual
review used admin-access.png, member-access.png, and member-unavailable.png.
New mobile/RTL evidence is access-mobile.png and access-he.png. Before reference:
before-access-dialog.png. Five access labels are localized in all ten catalogs.

Used-by refinement: the main dialog replaces its large object count with a plain
inline row of DataSourceIcon + agent name, comma-separated, with no links or
pills. Counts are under Details. The gray account card and icon container are
removed; USelectMenu replaces the native menu; Refresh access is connector-neutral.

GET /connections/{id}/accessible-agents uses the existing connection-read gate
and get_accessible_data_source_ids resolver, including public agents and the
full-admin capability bypass. Only id/name/icon of accessible linked agents are
returned. The UI never falls back to the unfiltered agent_names field.

Run the backend permission regression:

```sh
cd backend
TESTING=true .venv/bin/python -m pytest tests/e2e/rbac/test_rbac_connections.py -k used_by_only -q --tb=short --disable-warnings
```

Before the endpoint: failed with 404. After: pass for an admin, a member with one
agent grant (hidden agent name absent), and connection-cascaded management access.
Browser checks cover plain-text names, member filtering, styled account menu,
management flows, mobile and RTL. Evidence: admin-access.png, member-access.png,
account-menu.png, access-mobile.png, access-he.png; before-used-by.png is the
previous layout. The four labels exist in all ten locales.

Inline last-checked refinement: the timestamp now follows the connection status
in the header and wraps on narrow screens. Removed Details and its counts;
standardized the dialog to a 16px title and 13px body/actions with 20px line height.
The browser regression verifies the visible header timestamp and absence of the
Details button; all management, access, RTL and mobile scenarios passed. Before:
before-inline-checked.png. After: admin-access.png and access-mobile.png.

Account layout redesign: replaces the Account dropdown with a fieldset of two
radio choices, My account and Organization account, with their permission meaning
visible. Users without an allowed choice see a single account explanation.
Personal sign-in/result is attached to My account; only an explicit shared result
(or system-auth result) can populate shared header health. Missing shared status
is not inferred from a personal token. Non-personal connectors omit this section.
Actions adapt to sign-in, signed-in and organization-account states. Used by is
secondary header context; management has a separate footer. Typography is 13px
body/actions, 12px supporting text, 16px title, with 36px action buttons.

The existing browser command also checks both radio selections, signed-out
admin/member views, shared success with missing personal authentication, and a
system-only SQLite view. Captures: before-account-redesign.png (before),
sign-in-access.png, shared-connected-sign-in.png, organization-access.png,
simple-access.png, admin-access.png, member-access.png and access-mobile.png.
Six new labels are present in all ten locale catalogs. This runs real components
with mocked API/provider boundaries; it does not sign into an external provider.

Final validation: Vue script/template compilation and git diff --check passed.
The full browser regression passed with the final layout, including radio
switching, personal and organization states, simple connectors and RTL/mobile.
During iteration one run timed out locating the sign-in action, and one repeat
lost access to the page body; the final unmodified-component run passed. The
harness now preserves the original error and attempts a bounded debug capture
rather than masking it with a second timeout. No external OAuth login was tested.

Inline account actions: Sign in now sits directly under the My account description
and replaces the redundant Sign in required line. Refresh access and Sign out
occupy that same area when signed in. Buttons use 12px medium-weight text with
36px minimum height. Actions sit outside the radio label, preserving native radio
and button interaction. Before: before-inline-sign-in.png. The browser regression
checks that Sign in belongs to the personal account block, precedes Organization
account visually, and renders at 12px. Existing labels are reused.
Validation passed: Vue compilation, diff whitespace check, and the complete
browser regression. Reviewed sign-in-access.png, admin-access.png and
access-mobile.png for alignment and compact typography.
