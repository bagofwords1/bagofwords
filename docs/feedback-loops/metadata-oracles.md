# Feedback Loop — WP10 "metadata oracles and minor leaks"

Pentest WP10 (findings 828741817, 829403296, 829411025, 829378772, 828959664).
Claim under test: five endpoints tell a caller something about objects they
cannot access (existence, agent names, eval results, agent context), or keep
serving a file the user removed.

Out of scope by decision: 829412008 (deleted instruction text in
`/builds/{id}/contents` keeps showing, intended; the #1324 scope filter stays)
and 828812783 (distinct "account disabled" login message, accepted risk).

## Root cause (validated)

| Finding | Root cause |
|---|---|
| 828741817 rollback oracle | `routes/build.py` `rollback_to_build` ran `_enforce_build_ds_access` (which names the agent in its 403, `core/permissions_decorator.py` ~L418) on any build id, before `build_service.rollback_to_build` checked the org. `get_build_instruction_ids` / `get_build_data_source_ids` take a bare id. An org-B admin got 403 "does not belong" for an org-A build vs 404 for a random id. |
| 829403296 eval baseline | `TestRunService.compare_runs` picked the default baseline from runs in the org that share one case, with no read check. Both `GET /tests/runs/{id}/compare` and the `get_eval_run` tool (`compare_to_previous`) use it. |
| 829411025 project agents | `ProjectService._to_schema` listed every default agent of the project. |
| 829378772 enhance | `_build_data_source_context` used caller-supplied ids filtered only by org. |
| 828959664 avatar | `DELETE /users/me/avatar` and re-upload only changed `users.image_url`; the PNG stayed in `uploads/avatars` and `GET /users/avatar/{key}` is public. |

## Loop A — pytest (no external services)

`backend/tests/e2e/rbac/test_metadata_oracles.py` (LLM stubbed for enhance;
eval results inserted directly because they only come from a live run).

```bash
cd backend && ENVIRONMENT=production TESTING=true .venv/bin/python -m pytest -q --db=sqlite \
  --disable-warnings tests/e2e/rbac/test_metadata_oracles.py
```

Fix reverted (`git checkout -- backend/app`): `8 failed, 3 passed` (the 3 are
regression guards: own-org rollback, admin baseline, cross-user avatar).
Fix applied: `11 passed`.

## Loop B — live stack over HTTP

Backend on :8047 via `metadata-oracles/launch.py` (a sibling stack held
:8000; the launcher enables multiple orgs like the rbac conftest). Fresh
migrated SQLite per run. `metadata-oracles/probe.py` seeds org A (admin,
member, "Payroll Secret Agent", "Sales Agent"), org B with an outsider who
manages instructions on an org-B agent, then probes.

| Probe | Before (`before.txt`) | After (`after.txt`) |
|---|---|---|
| Rollback org-A build as org-B agent manager | 403 **`No access to agent "Payroll Secret Agent"`** | 404 `Build not found` |
| … random id, same caller | 403 `Org-level 'manage_instructions' required` | 404 `Build not found` |
| Rollback org-A build as org-B admin | 403 `Build does not belong…` (vs 404 random) | 404 (same as random) |
| Rollback in own org (admin A) | 200 | 200 |
| Compare baseline, member with manage_evals on Sales only | **"HIDDEN payroll baseline"** | "visible baseline" |
| Compare baseline, admin | "HIDDEN payroll baseline" | unchanged |
| `GET /projects/{id}` agents as member | **Payroll Secret Agent**, Sales Agent | Sales Agent |
| … as admin | both | both |
| Replaced avatar old URL | **200** | 404 |
| Avatar URL after DELETE | **200** | 404 |
| Current avatar / upload after delete | 200 / 200 | 200 / 200 |

Enhance was not probed live: it needs a real LLM. Loop A covers it with the
LLM stubbed at the boundary, asserting the context handed to the model.

## The fix

- `routes/build.py`: rollback resolves the build in the caller's org first;
  missing and foreign both return 404 `Build not found` before any permission
  check runs.
- `services/test_run_service.py` `compare_runs`: the baseline (default or
  `against_run_id`) needs the same read rule as the main run: org-level
  eval admin, or every case of the run visible. Unreadable default candidates
  are skipped, so the comparison falls back to the newest readable one.
- `services/project_service.py`: `_to_schema` filters default agents with
  `filter_user_visible_data_sources`. `set_default_data_sources` keeps
  existing defaults the caller cannot see, so a save from the filtered UI
  does not silently drop them.
- `services/instruction_service.py`: enhance context only includes agents the
  caller can see; others are dropped without an error naming them.
- `routes/user_profile.py`: removing or replacing an avatar deletes the old
  file (only `{user_id}-*.png` in the avatars dir; same-image re-upload keeps
  its file).

## What this proves / regression notes

- Response shapes are unchanged (lists filtered, nothing restructured).
  Frontend consumers checked: `BuildExplorerModal.vue` (rollback),
  `pages/projects/[id]/index.vue` (`project.data_sources`),
  `Instruction*CreateComponent.vue` (enhance), `UserProfileModal.vue` (avatar).
- Browser screenshots were not taken: the sandbox was memory-bound by sibling
  stacks, and no frontend file changed.
