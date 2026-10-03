# Feedback Loop — "which data connectors does the enterprise license gate?"

There were two different answers. The UI locks every connector whose registry
entry sets `requires_license="enterprise"` (28 types). The server, though,
checked a separate hardcoded list of 9. So 19 connectors were locked only in
the UI, and a direct `POST /api/connections` could create them on a community
install. The same change also makes Kubernetes, Splunk, OneDrive, Google
Drive, Outlook Mail and Gmail free (community tier), by product decision.

## Root cause (validated)

- UI lock reads the registry field `requires_license` served by
  `list_available_data_sources()` (`backend/app/schemas/data_source_registry.py`)
  — `frontend/components/AddConnectionModal.vue:387`,
  `frontend/components/datasources/DataSourceGrid.vue:108`,
  `frontend/pages/onboarding/data/index.vue:162`.
- Server gate `is_datasource_allowed()` (`backend/app/ee/license.py`) checked
  a separate hardcoded `ENTERPRISE_DATASOURCES` list of 9 types. It is called
  from `connection_service.create_connection`,
  `data_source_service.create_data_source` (both modes) and
  `agent_yaml_service._check_licenses`.
- Types in the registry but not in the list were locked in the UI and accepted
  by the API: `powerbi_report_server`, `qlik_sense`, `qlik_sense_onprem`,
  `documentum`, `sharepoint`, `sharepoint_onprem`, `sharepoint_lists`,
  `onedrive`, `onenote`, `outlook_mail`, `gmail_mail`, `google_drive`, `timbr`,
  `timbr_a2a`, `sisense`, `oracle_bi`, `infor_olap`, `infor_epm`,
  `analysis_services`. A license's `ds_<type>` features also had no effect
  on these types.

## Loop A — deterministic reproduction (no external services)

Tests: `backend/tests/e2e/test_license.py::TestDataSourceLicenseGateMatchesRegistry`

1. For every registry type, unlicensed `is_datasource_allowed(t)` must equal
   `requires_license != "enterprise"`.
2. With an enterprise license, every registry type is allowed.
3. Unlicensed `POST /api/connections` must return 402 for every enterprise
   registry type, and must not return 402 for the six community connectors.
   For the four OAuth ones it must not return 402 with `user_required` either.

```bash
cd backend
uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
mkdir -p db
uv run pytest tests/e2e/test_license.py -k GateMatchesRegistry -q
```

Before the fix (`git stash push -- app/`):

```
E       AssertionError: assert {'analysis_se...erprise', ...} == {}
E         Left contains 19 more items:
E         {'analysis_services': 'enterprise',
E          'documentum': 'enterprise',
E          'gmail_mail': 'enterprise',
E          'google_drive': 'enterprise',
E          'infor_epm': 'enterprise',...
E           AssertionError: ('powerbi_report_server', 400)
E           assert 400 == 402
2 failed, 1 passed
```

Unlicensed, a Power BI Report Server connection got past the license check
and failed only at config validation (400).

## The fix

- `backend/app/ee/license.py` — the hardcoded list is replaced by
  `enterprise_datasources()`. It builds the set from `REGISTRY` entries with
  `requires_license == "enterprise"`, so the registry is the single source of
  truth. It imports the registry lazily to keep `app.ee` free of an import
  cycle. A module `__getattr__` still serves `ENTERPRISE_DATASOURCES` for old
  imports.
- `backend/app/ee/__init__.py` — exports `enterprise_datasources` instead of
  the constant. Re-exporting the constant would load the registry eagerly.
- `backend/app/schemas/data_source_registry.py` — `requires_license` removed
  from `kubernetes`, `splunk`, `onedrive`, `google_drive`, `outlook_mail` and
  `gmail_mail`.
- `backend/tests/unit/test_kubernetes_client.py` — now asserts Kubernetes is
  community tier.

After the fix:

```
3 passed, 20 deselected
```

## Loop B — live UI confirmation

Stack: `tools/agent/boot_stack.sh --dev`, then `tools/agent/seed_org.py`, with
no license configured. Screenshots of the Add Connection modal, before and
after: `media/pr/free-connectors-license-gate/`. The frontend is unchanged.
Before, the six connectors show the enterprise lock. After, they are
selectable, and the remaining enterprise connectors (Power BI, Tableau,
SharePoint, …) are still locked.

## What this proves / regression notes

- The server gate and the UI lock now come from the same field, and test 1
  fails if they drift again for any registry type, including new ones.
- The 19 drifted types are now enforced by the server (402 when unlicensed).
  This tightens behavior for anyone who created them through the API without
  a license. Existing connections are not touched, because the gate runs only
  on create and attach.
- The per-user (OAuth) gate `_user_auth_needs_enterprise` is unchanged. It
  gates only `data_shape == "tables"`, so per-user sign-in for OneDrive,
  Google Drive, Outlook and Gmail stays free.
