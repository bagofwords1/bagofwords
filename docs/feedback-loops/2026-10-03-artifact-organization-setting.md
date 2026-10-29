# Artifact resources: default-on organization setting

The supplied conversation trace rejected a persistent blog because the process-level resource flag was absent. Resource creation previously checked that flag after generation. The replacement is `OrganizationSettings.config.enable_artifact_resources`, an ordinary editable feature defaulting to true and rendered by the existing AI settings page.

## Enforcement

`artifact_resource_policy.py` reads the current stored config, defaulting to enabled when the setting is absent. `ArtifactResources.open` applies that policy after report visibility checks, so HTTP runtime, publication, schema authoring, MCP and ongoing stream authorization share the same tenant boundary. Existing identity, group and resource policies still apply. Organization settings updates retain the existing `manage_settings` permission and auditing.

Resource-backed creation checks availability before generation or creating a pending version, and checks again through the service when applying resources. Reading a legacy artifact still works when disabled and reports `resources_enabled: false`. Disabling resources does not erase records or files; enabling restores access. The old `BOW_ARTIFACT_RESOURCES_ENABLED` variable no longer controls availability. The independent server read-only switch remains supported.

## Reproduction and results

Before the fix, the API regression failed because organization settings had no resource feature and unconfigured resource access was disabled. Run from `backend`:

```sh
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true .venv/bin/python -m pytest tests/e2e/test_artifact_resources.py tests/e2e/test_artifact_resource_tool_errors.py tests/e2e/test_artifact_resource_requirements.py tests/e2e/test_create_artifact_replaces.py tests/unit/artifact_resources -q
```

SQLite: all 57 tests passed. PostgreSQL: all 57 tests passed (`--db=external`, disposable database in `TEST_DATABASE_URL`). Checks include administrator/member authorization, independent organizations, default-on behavior, disable/re-enable with retained records, early rejection without a model, and ignoring the retired environment flag.

The seeded application's existing AI settings page displays the new enabled control. Screenshot: `media/pr/artifact-resources/org-settings-after.png`. All ten locale catalogs include the setting. No frontend component or bundle change is required for the English backend-label fallback to display the control on an already-built deployment.

## Deployment

A backend source patch can be applied to the existing container and activated with an app restart; no image build or database migration is needed. Back up original source and startup configuration first and verify source hashes against the deployed base. A container-layer patch survives restart but is lost on container recreation; deploy an image containing this commit before replacing the container.

Persistent uploads require `BOW_ARTIFACT_STORAGE` on a mounted private volume and the existing stable encryption key. For the manual bow deployment, use `/app/backend/uploads/artifact-resources` on the existing uploads volume, preserving the setting in the compose configuration as well as the current startup script. Do not change encryption keys or expose credentials.


### Manual deployment result — 2026-10-03

Patched `bow-app` from the matching `60a0121a7` base to backend source from `cfc3ec14a`, then restarted it. The original container image ID is unchanged and Docker reports healthy. Source hashes match the tested patch. The trace's organization is enabled; live service record creation/read succeeded and was rolled back. The running process points to the persistent uploads volume; an encrypted file round-trip succeeded and its probe was removed. The compose storage setting is saved for future deployments. No model call or customer-record edit was performed for this smoke check.

Original source/startup/configuration and a rollback script are stored on `bow` at `/home/ubuntu/artifact-org-backup-20261003T084938Z`. Container recreation still requires the updated image or reapplying this source patch. Existing frontend bundles display the new control using their backend-label fallback; translated labels take effect with the next frontend bundle build.
