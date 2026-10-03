# Resource authoring UI and context

Resource tool calls previously rendered as raw tool names, while their conversation digests omitted resource identity and changes. Analytics occupied a separate toolbar button with a full-width native selector. This change gives resource calls an expandable card, moves Data & analytics into More, and uses a compact headless date selector.

## Behavior

- `manage_artifact_resources` reports the committed action, stable artifact/resource identity, name/kind, revision, full affected definition, changed sections and before/after values. Failure observations carry a code, explanation and `committed: false`. No unrelated resource definitions or user record/file contents are copied into a mutation response. Read operations still return the visible catalog.
- Resource cards use that result and retain compatibility with older results through tool arguments. They distinguish failed operations even if transport status is success, show schema/change details, and open the existing read-only Data explorer.
- Both message-context paths use `digest_artifact`. It preserves stable IDs, historical revisions, bounded schema/permission summaries and failures. A re-read reminder prevents historical permissions from being treated as current authority. Persisted observation projections prioritize resource identity and mutation outcome under their existing size budget.
- The publication tool, MCP registration, endpoints and service are removed. Shared reads use their existing version-list behavior. Existing publication rows are retained inertly for database compatibility; they no longer filter shared versions. No destructive migration or data deletion is needed. Blog-post publishing remains a record/policy operation.

## Basic verification

From `backend`, with normal development dependencies:

```sh
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true .venv/bin/python -m pytest tests/e2e/test_artifact_resources.py tests/e2e/test_artifact_resource_tool_errors.py tests/e2e/test_artifact_resource_requirements.py tests/e2e/test_create_artifact_replaces.py tests/unit/artifact_resources -q
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true .venv/bin/python -m pytest tests/unit/artifact_resources/test_digest.py tests/unit/test_message_context_tool_result_projection.py -q
```

Observed: 59 focused tests passed, followed by 5 digest/projection tests (including large-observation and legacy-result cases) and 16 existing message-context regression tests. The first pass exposed three tests comparing the old failure dictionary exactly; those now assert failure, no commit and a structured error code. A retained-pin fixture verifies old pins cannot hide newer shared versions and the removed endpoint/tool is unavailable.

Live Chromium verification used the seeded document report for the More menu, analytics panel and 7-day range selection. A temporary local fixture page rendered the actual resource-card component with synthetic success/failure responses, including a logical failure with successful transport. English/light and Hebrew/dark screenshots were inspected. Browser errors: zero. Production frontend build passed. The temporary route and driver were removed before the production build. The video and screenshots are under `media/pr/artifact-resources/` (`analytics-before/after`, `analytics-range-after`, `resource-cards-after`, `resource-cards-he`, `resource-ui-flow.webm`). This is component/UI evidence, not a new model evaluation.

## Manual follow-up / deployment

The user will perform broader manual journeys, including real model follow-ups and context compaction. No paid-model evaluations or production deployment are part of this pass. Deploy backend and rebuilt frontend together: a backend-only hot patch cannot deliver the new cards/menu. Existing database publication rows can remain; reverting to an older image would reactivate its pin interpretation.
