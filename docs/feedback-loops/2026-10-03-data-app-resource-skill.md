# Data app resource guidance

The shipped `data-app-design` skill described analytical layout and query controls but did not teach persistent app workflows. The SDK reference already supplied the resource contract. This update adds workflow guidance to the existing skill instead of duplicating APIs or introducing another skill to discover.

## Inspection and change

At parent commit `b50b94f4e`, `backend/app/ai/skills/library/data-app-design.md` had no resource-authoring workflow, and `backend/app/ai/agents/planner/data_app_authoring.py` said there was no generic writeback/action API without distinguishing app records from analytical sources. This was a guidance gap found through source inspection, not a reproduced model failure.

The catalog entry keeps its stable key, category, default-enabled state, order and modes. Version 1.1 is titled Data apps and advertises saved records, uploads, AI streaming, permissions and analytical data. It teaches create versus edit, schema versus record operations, existing identities/groups, sharing versus resource permissions, conflict handling and realistic runtime verification. The authoring brief now includes persistent app requirements and clarifies the analytical writeback boundary. Exact SDK signatures remain in `artifact_sdk_reference.py`.

## Verification

From `backend`:

```sh
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true .venv/bin/python -m pytest tests/unit/test_skill_catalog_library.py tests/e2e/test_skill_catalog.py -q
```

Observed: **41 passed** on SQLite. The authoring module compiled and `git diff --check` passed. An initial invocation from the repository root failed before collection because settings resolve `../VERSION`; the command above uses the required backend working directory.

These existing tests cover catalog parsing, discovery, default installation, customization detection, explicit updates and disabled defaults. They do not prove model skill selection or successful generated apps; no paid model evaluation was repeated for this text update.

## Rollout

Deploy the catalog file and authoring guidance together. Existing organization skill copies are not automatically overwritten. Administrators use the existing catalog update action and review customization warnings; missing default installations receive the current version through the existing default installer. No new migration, auto-update policy, or changes to organization instructions are included. Catalog source and authoring guidance were deployed to bow with the shared-artifact loading fix on 2026-10-03. Existing installed copies still require an explicit catalog update.
