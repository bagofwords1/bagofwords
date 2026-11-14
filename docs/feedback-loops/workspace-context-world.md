# World in Agents

An organization admin can enable **Show World in Agents** under Settings → General. Members can then open the globe in Agents and inspect the relationships their account can see.

## Reproduce on the base

The shared-folders branch at `b55fb5d2c` exposed individual resource lists. Its router registration in `backend/main.py` had no World endpoint. Copy the World access test into a disposable checkout at that revision and request the overview as an authorized agent owner. The baseline run failed with HTTP 404 where the graph test expected 200.

## API verification

Run in a disposable checkout with Python 3.13. The test harness creates its own database. External model and connector credentials are unnecessary.

```sh
cd backend
uv sync --frozen --extra dev
mkdir -p db
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db uv run python -m pytest -q tests/e2e/rbac/test_world.py
```

For PostgreSQL, use a new, empty test database and run the same command with `--db=external`, setting `TEST_DATABASE_URL` to that database. Both runs passed all 11 tests. The cases cover the off-by-default API, permission to enable it, inaccessible agents, private reports, files, memory, tool policies, cross-org requests, direct folder grants, shared instructions, evals, and a 300-agent overview.

`backend/app/routes/world.py:21` checks membership and the setting for every page. The service projects relationships from the underlying resource services. Folder expansion uses the exact folder knowledge listing at `backend/app/services/world_service.py:80`. Its edge helper at line 117 requires both endpoints on the authorized page.

## Browser verification

On a disposable running stack, run the feature project from the frontend directory.

```sh
PLAYWRIGHT_BASE_URL=http://localhost:3000 yarn playwright test tests/data_sources/world.spec.ts --project=features
```

The spec exercises the real General settings form, globe link, graph search, folder expansion, folder destination and disabled deep link. It restores the initial setting and removes its synthetic folder. An additional Playwright pass signed in as a separate query-only member, checked access with the setting enabled, and verified redirection after the admin disabled it. English and Hebrew captures are under `media/pr/workspace-context-world/`.

The live pass caught an outdated dashboard-list call in the prototype. The projection now supplies the dashboard service's current `ListQuery`. The all-layers API test exercises this for an admin and a member on both databases.

## Loading behavior

The initial overview includes the folder hierarchy, agent placements and connection links. Workspace layers load in batches. Agent internals, exact-folder knowledge and report activity can be expanded individually. The search field filters loaded items. The load controls fetch further pages and agent or folder expansions. Personal memory requires an explicit toggle. The feature adds one organization setting and reads existing resource records without a migration.
