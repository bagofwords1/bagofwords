# Artifact runtime review fixes

PR #1220 review identified compatibility and admission issues in the artifact resource foundation. These fixes preserve artifact identity and existing data; they do not add execution history.

## Findings and fixes

| Finding | Change | Evidence |
| --- | --- | --- |
| Opaque iframe silently drops messages addressed to the host origin | Send to the specific iframe window with `*`; validate the source window on receipt | Real Chromium shared data delivery and parent-only theme update |
| Plain HTTP lacks `crypto.randomUUID` | Use secure `getRandomValues` for the bridge nonce and provide an RFC4122 UUID fallback to generated code | Chromium on an insecure HTTP origin |
| Streams consume all ordinary request slots | Separate bounded stream admission (default 4) from ordinary requests (default 16) | Exhaust stream capacity, receive 429 for another stream, ordinary read still succeeds |
| Rebuilding resubmits existing resource definitions | Reuse identical definitions under the artifact lock; preserve SDK metadata and rows; reject implicit schema/policy changes | Rebuild both with and without definitions; verify saved data, revision and conflict behavior |
| Long resource names exceed idempotency-key bounds | Hash the name component in creation keys | Full API/tool flow with a 63-character resource name |
| Deleted resources permanently consume the live resource quota | Count only live resources toward 50; keep up to 1,000 reserved identities | 51 create/delete cycles, new creation succeeds, old name remains reserved |
| Proxy address collapses anonymous identity | Enable trusted proxy handling explicitly; development proxy appends client address | Trusted/untrusted/spoofed proxy-chain cases |
| Opaque fonts fail CORS and legacy external images are blocked | Public font-only CORS in production and development; legacy HTTPS images restored; SDK resource apps retain restricted image policy | Production static server tests, real development font request, Chromium image policy tests |

Reserved names intentionally cannot be reused: old artifact versions must never silently bind to a different resource. The larger identity limit remains bounded. This is a live-capacity fix, not unbounded churn support.

Deployments behind a proxy must set `FORWARDED_ALLOW_IPS` to their actual trusted ingress addresses/CIDRs. The default trusts only loopback. Do not trust arbitrary forwarded headers or blindly configure `*`. Ingress forwarding and deployed rate-limit behavior still need deployment-specific qualification; the tests validate the trust boundary locally.

## Reproduce and verify

Run from `backend` with the normal development dependencies:

```sh
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true .venv/bin/python -m pytest tests/e2e/test_artifact_resources.py tests/e2e/test_create_artifact_replaces.py tests/unit/artifact_resources -q
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true .venv/bin/python -m pytest tests/unit/test_openai_reasoning_requests.py tests/unit/test_artifact_design_system.py -q
```

Repeat the first command against a disposable PostgreSQL database using `--db=external` and `TEST_DATABASE_URL` for that database. Do not point migration tests at application data.

From `frontend`:

```sh
node tests/unit/artifactOpaqueFrame.mjs
node tests/unit/artifactFormIsolation.mjs
node tests/unit/artifactResourceSdk.mjs
```

Observed before fixes: long-name/rebuild and admission regressions failed (3 failures); actual Chromium showed undefined `randomUUID` on plain HTTP and discarded origin-targeted messages. Prior CI independently reported 15 stream-cleanup failures and a runtime-version mismatch.

Observed after fixes: the resource/rebuild/admission/static/proxy suite passes **47 tests on SQLite and 47 on PostgreSQL**. The opaque-frame and form-isolation browser tests pass. SDK, verification delivery and viewer hydration checks pass. The production frontend build succeeds. The targeted provider/runtime CI regressions pass 61 tests.

CI cleanup corrections keep the iframe asset version aligned with the existing runtime generation and accept both SDK stream `close()` and async-generator `aclose()` while retaining cancellation cleanup. The independent SDK cache key advances for its UUID compatibility change.

## Visual evidence and limits

`media/pr/artifact-resources/review-fonts-before.png` and `review-fonts-after.png` show the existing generated document app before/after this review pass. The screenshots establish layout continuity; HTTP and browser assertions establish font/image policy behavior. They are not proof of ingress behavior or production capacity.

The earlier real Luna/Sol completion results remain in `2026-10-02-artifact-live-completions.md`; no paid-model rerun was needed for these transport/compatibility changes. Full repeated release qualification and production load/failover checks remain outstanding. The subsequent organization-settings change makes resources enabled by default; admins can disable them per organization. The PR remains draft.
