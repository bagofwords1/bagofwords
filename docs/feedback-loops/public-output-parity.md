# Feedback Loop — "Public /r/{report_id}/export_html leaks identity-withheld query rows" and "PII redaction bypass in /api/c/{token}"

Pentest WP7 (findings 829389751, 829414651). Claim under test: two public
surfaces serve more than the public report view does.

1. An anonymous reader of a shared dashboard sees **no rows** for an
   identity-scoped query on the page (`snapshot_withheld: true`), but the
   "Download HTML" export of the same dashboard embedded the owner's rows.
2. With PII protection on, the owner's own timeline masks emails and custom
   patterns, but the public shared-conversation API served them raw.

Lead decision: no new policy. Public outputs reuse the code path the public
report view already uses, so the three surfaces cannot drift.

## Root cause (validated)

- **Export.** `collect_visualizations` read `step.data` directly
  (`backend/app/services/artifact_payload.py:95` before the fix), and the public
  route called it with no reader (`backend/app/routes/report.py:972`). The
  route's only gate was `report_snapshot_withheld`, the report-level
  credential check. That check does not cover identity-scoped queries, which
  `resolve_step_data` withholds per step
  (`backend/app/services/viewer_data_policy.py:117`, `step_identity_scope`).
  The JSON view (`ReportService.get_public_step`) goes through
  `resolve_step_data`; the export did not.
- **Conversation.** `ReportService.get_public_conversation` built its payload
  from raw columns (`report_service.py:3752` prompt, `:3689` block content,
  `:3721` tool result) and never entered `display_redaction`. The owner path
  (`CompletionService.get_completions_v2`) does enter it, and the v2
  serializers run `redact_text_display`, `redact_deep_display` and the related
  helpers.

## Loop A — deterministic reproduction (pytest, no external services)

`backend/tests/e2e/test_public_output_parity.py`:

| Test | Asserts |
|---|---|
| `test_public_export_withholds_identity_scoped_rows_like_the_json_view[identity, input_identity_default]` | export rows == public JSON rows == [] |
| `test_public_export_matches_the_json_view_for_plain_shared_rows` | plain rows still exported, == JSON view (guard) |
| `test_owner_public_export_keeps_their_own_identity_scoped_rows` | owner keeps full rows (guard) |
| `test_shared_conversation_is_masked_by_the_same_redactor_as_the_owner_view` | no raw email or EMP id; text == `redactor.redact_display(...)` for the org's redactor |
| `test_shared_conversation_is_unchanged_when_pii_protection_is_off` | PII off means content unchanged (guard) |

```bash
cd backend && ENVIRONMENT=production TESTING=true .venv/bin/python -m pytest -q --db=sqlite \
  --disable-warnings tests/e2e/test_public_output_parity.py
```

Fix reverted (`git checkout origin/ai/friendly-dijkstra-2e8qmj -- backend/app`):

```
FAILED ...test_public_export_withholds_identity_scoped_rows_like_the_json_view[params0]
FAILED ...test_public_export_withholds_identity_scoped_rows_like_the_json_view[params1]
FAILED ...test_shared_conversation_is_masked_by_the_same_redactor_as_the_owner_view
3 failed, 3 passed
```

Fix applied: `6 passed`.

## Loop B — live stack (seeded, anonymous HTTP + browser)

The backend runs on :8010 through `public-output-parity/launch.py`. The
launcher applies the same fake enterprise license as `tests/e2e/conftest.py`,
because `pii_protection` is license-gated. The frontend is `nuxt dev --port
3010` with `BOW_API_TARGET=http://127.0.0.1:8010`. A sibling stack was
already on :8000/:3000, so this loop did not use `boot_stack.sh`'s ports.

1. `tools/agent/seed_org.py --base-url http://localhost:8010` creates the admin
   and the org.
2. `PUT /api/organization/settings` enables `pii_protection` with an
   `EMP-\d{4}` custom rule.
3. `public-output-parity/seed_wp7.py` seeds three things:
   - "Identity dash", an identity-param query with rows `SECRET-ROW-*`;
   - "Plain dash", rows `PUBLIC-ROW-*`;
   - a public conversation that contains `jane.doe@acme-corp.com` and `EMP-4821`.

   Both dashboards are page artifacts with `artifact_visibility=public`.
4. `public-output-parity/probe.sh ids.json` makes anonymous `curl` calls.

| Probe (anonymous) | Before (`before.txt`) | After (`after.txt`) |
|---|---|---|
| Identity dash, JSON step | rows 0, withheld True | rows 0, withheld True |
| Identity dash, `export_html` | 200, **SECRET-ROW x4** | 200, **SECRET-ROW x0** |
| Plain dash, JSON / `export_html` | 4 rows / PUBLIC-ROW x4 | 4 rows / PUBLIC-ROW x4 |
| `/api/c/{token}` | raw email **True**, EMP-4821 **True** | raw email False, EMP False, non-PII text True |
| Owner (Bearer) `/r/{id}/export_html` | n/a | SECRET-ROW x4 (entitled) |
| Owner in-app `/api/artifacts/{id}/export/html` | n/a | SECRET-ROW x4 (unchanged path) |

Browser, after the fix, with `tools/agent/capture.mjs`. The PNGs are in `public-output-parity/`:

- `public-report-plain.png`: the public report renders its chart and table.
- `public-report-identity.png`: the public report shows the existing "Sign in to see your data" state.
- `export-plain-offline.png`: the downloaded plain export renders offline with all 4 bars.
- `export-identity-anonymous.png`: the anonymous identity export shows "rows: 0", the same as the page.
- `shared-conversation.png`: the shared page renders `[REDACTED_EMAIL]` / `[EMP]`, and "Q3 revenue was $1.2M." is intact.

Environment note: the first browser captures of `/c/...` failed with Vite
`504 Outdated Optimize Dep`. The cause was `frontend/node_modules`, which is
symlinked and shares its Vite cache with the sibling dev server. Restarting the
dev server cleared it. This is unrelated to the change, which touches no
frontend code.

## The fix

- `artifact_payload.py` adds `viewer=`. When it is set, every step's rows go
  through `viewer_data_policy.resolve_step_data`, which is the
  `get_public_step` path. The same applies to `options_source` option lists,
  and `applied_params` goes through `redact_applied_params`. When it is not set
  (`UNSCOPED`), the in-app, PDF and edit-preview callers behave exactly as
  before.
- `html_export_service.build_standalone_html(..., viewer=)` passes the reader
  through. `routes/report.py` (public `export_html`) passes `viewer=user`.
- `report_service.get_public_conversation` now runs inside
  `display_redaction(report.organization_id, ...)` and applies the owner
  serializer's helpers:
  - `redact_prompt_display` to the prompt;
  - `redact_text_display` to block content and reasoning, and to the
    plan-decision text, which echoes the block;
  - `redact_deep_display(project_tool_result_for_ui(...))` to the tool result.

## What this proves / regression notes

- The export can no longer embed rows that the JSON view withholds for the
  same reader. The owner still gets full data, and plain shared dashboards
  still export charts and tables.
- The shared transcript is masked by the same redactor instance and helpers as
  the owner view. With PII off, it is byte-identical to before.
- Related suites passed: 162 + 97 tests, 3 skipped (browser-only offline tests
  without Chromium in pytest), 0 failures. See the PR for the file list.
- Not changed: the public PDF and PPTX exports
  (`/r/{id}/export_pdf|export_pptx`) render through `report_pdf_service`. Both
  routes already refuse credential-scoped snapshots with 409, but neither was
  re-verified here for identity-scoped queries. They are a candidate follow-up
  for the same `viewer=` threading.
