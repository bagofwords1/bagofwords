# Feedback Loop — Preview backend is unavailable in the container

Production verification could not open a saved page even while the normal application API worked. This follow-up removes the internal network dependency; it does not change the browser page origin.

## Root cause (validated)

`backend/app/services/artifact_preview_service.py:64` defaulted internal API requests to `127.0.0.1:8000`. On the production host, `/app/start.sh:105` runs Uvicorn on port 3000 and `BOW_ARTIFACT_BACKEND_URL` was unset. A read-only probe inside the running container returned connection refused on 8000 and an HTTP response on 3000. The deployed source maps that connection failure to the exact reported message.

## Reproduction and verification

From `backend`:

```sh
TESTING=true .venv/bin/python -m pytest tests/e2e/test_artifact_browser_policy.py --db=sqlite -q
```

`test_preview_reads_authenticated_api_without_a_backend_listener` seeds a user, report and page through the API, enables verification, and makes outbound HTTP unavailable. It opens the real preview service and checks that authenticated artifact/query reads succeed without a backend URL setting. Before the change it fails with `PreviewUnavailableError`.

## Fix

Use `httpx.ASGITransport` against the running FastAPI application. A synthetic base URL satisfies HTTP request construction but is never resolved or connected to. The same bearer token, organization header, routes, middleware and permission checks still apply. ASGI errors become HTTP 500 responses and retain the existing unavailable-error handling. No second server or application lifespan is started.

## Scope

This verifies the internal API connection and existing authorization contracts. Browser rendering still uses the separate page origin. It does not prove every generated dashboard interaction works, and a successful query alone is not evidence of successful UI filtering.

## Observed result

Before: the no-network regression failed with `PreviewUnavailableError` (1 failed, 9 deselected). After: the full focused policy/API suite passed, **10 passed**. This includes opt-in/privacy, exact-page authorization, non-admin revocation, logout, session cleanup, and the real in-process authenticated reads. Production has not been modified by this follow-up.
