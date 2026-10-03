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

SQLite: 56 tests passed before adding the final tenant-isolation test; both new organization-policy tests then passed together. PostgreSQL: all 57 tests passed (`--db=external`, disposable database in `TEST_DATABASE_URL`). Checks include administrator/member authorization, independent organizations, default-on behavior, disable/re-enable with retained records, early rejection without a model, and ignoring the retired environment flag.

The seeded application's existing AI settings page displays the new enabled control. Screenshot: `media/pr/artifact-resources/org-settings-after.png`. All ten locale catalogs include the setting. No frontend component or bundle change is required for the English backend-label fallback to display the control on an already-built deployment.

## Deployment

A backend source patch can be applied to the existing container and activated with an app restart; no image build or database migration is needed. Back up original source and startup configuration first and verify source hashes against the deployed base. A container-layer patch survives restart but is lost on container recreation; deploy an image containing this commit before replacing the container.

Persistent uploads require `BOW_ARTIFACT_STORAGE` on a mounted private volume and the existing stable encryption key. For the manual bow deployment, use `/app/backend/uploads/artifact-resources` on the existing uploads volume, preserving the setting in the compose configuration as well as the current startup script. Do not change encryption keys or expose credentials.
