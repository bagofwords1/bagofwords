# Shared artifact loading and record request validation

A saved resource app rendered in the editor but its shared page said “No dashboard available.” The app also failed to load records. These were independent failures, not evidence of a missing collection.

## Reproduced causes

The publication-removal change left `published_version_clause()` in `ReportService.get_public_artifact` after deleting its definition. Live logs confirmed the artifact list returned 200 while artifact detail raised NameError and returned 500. The earlier pin regression tested listing alone; the deployment health/static checks also missed detail loading. This was a regression introduced by this PR's publication removal.

The generated app requested `limit: 200` and `orderBy: '-updated_at'`. RecordRequest permits limits 1–100 and ID/creation-time ordering. Both validation failures reproduced locally; live record requests returned 422. The preview's separate blocked context request did not establish the cause of the live error.

## Reproduce and verify

From backend:

```sh
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true .venv/bin/python -m pytest tests/e2e/test_artifact_resources.py -q --disable-warnings
```

Extending the retired-pin test to fetch both old and new artifact versions through the shared HTTP detail endpoint reproduced NameError before the fix. After removing the stale predicate: **23 passed**. Existing sharing authorization remains in place.

From repository root:

```sh
node frontend/tests/unit/artifactResourceSdk.mjs
node frontend/tests/unit/validationErrorMessage.mjs
node frontend/tests/unit/artifactOpaqueFrame.mjs
node frontend/tests/unit/artifactFormIsolation.mjs
```

Invalid list-option assertions failed before the SDK change and passed afterward in offline, explicit fixture and live-transport modes. Validation occurs before transport, so live requests use the same bounds. Structured validation errors expose field paths/messages without echoing inputs. Existing browser isolation and form tests passed. Production `npm run generate` passed.

The shared page checks both list and detail fetch errors instead of treating a failed request as an empty report. Before/after screenshots use the generated app with a synthetic list response and a 500 detail response, no customer data. Read-only previews no longer request live runtime context. The SDK reference now explicitly documents pagination bounds.

## Limits

These checks do not repeat paid-model evaluations or prove all generated apps comply with the SDK. Fixture interactions are not evidence of live permissions or persistence. The original generated app requires a new version using supported list options; SDK validation does not rewrite saved source automatically.

The guarded Kanban repair was applied locally to the trace source and parsed as JSX, then rendered in Chromium with explicit synthetic SDK fixtures. Task creation succeeded. It uses supported sorting and 100-row cursor pagination with a Load more control; searches and counts explicitly describe loaded records. This is fixture verification, not a live mutation test.

Deployment: backend and generated frontend applied together to bow without changing its container image. Backup and rollback script: `/home/ubuntu/artifact-experience-backup-20261003T132849Z`. This deployment also includes the Data apps v1.1 catalog source; existing organization skill copies still require an explicit update.

Post-deployment: container healthy, health/sign-in HTTP 200, all 2,126 frontend hashes and 88 served entry assets matched. The guarded app repair appended Kanban version 2 through the normal version service, keeping the old version and stable resource identity. No records or resource definitions were changed.

Live read-only verification passed through `ReportService.get_public_artifact` as the report owner and `ArtifactResources.records` with the corrected list options. This checks real stored state and authorization services without printing or mutating task contents; it is not a complete authenticated browser journey.
