# Feedback loop — embedded chat loses tools, reorders calls, and loads unnecessary libraries

A published agent app must preserve its agent surface after a scheduled wait, show text and tool calls in occurrence order, provide usable sign-in, and avoid waiting for chart libraries before a text-only conversation can render. This change also consolidates standalone controls into a vertical menu and hides that menu in an iframe.

## Validated causes

- `backend/app/ai/tools/implementations/wait.py` armed a job with report/user/org but no originating completion. `backend/app/services/wait_service.py:87` therefore resumed without the app platform context. Unknown context intentionally falls back to restrictive dashboard chat.
- `backend/app/services/artifact_conversations.py:358` replaces the previous representation, which split historical narration, answer, and tools into separate arrays. `frontend/public/libs/artifact-globals.js` rendered the answer before all tools and lacked an operation-name fallback for native MCP calls.
- `frontend/components/dashboard/SharedDashboardView.vue` prefetched and waited for ECharts for every artifact. `frontend/utils/artifactIframe.ts:242` replaces the previous plan, which unconditionally executed it, even for chat-only code.
- `ChatComposer` rendered `auth_required` as inert text. The published page exposed editing/dashboard chrome in two rows.
- MCP failure guidance always suggested correcting arguments, including rate-limit failures. A repository path was also easily mistaken for a BOW file id.

## Loop A — deterministic regression tests

Use Python 3.13 and install the normal backend dev dependencies. On macOS point `PW_CHROMIUM_PATH` at an installed Playwright Chromium; cloud sandboxes use their provisioned browsers. No live provider credentials are needed.

```sh
cd backend
BOW_DATABASE_URL=sqlite:///db/test.db TESTING=true .venv/bin/pytest \
  tests/unit/test_artifact_conversation_stream.py \
  tests/unit/test_artifact_conversation_results.py \
  tests/unit/test_wait_tool.py tests/unit/test_app_conversation_timeline.py \
  tests/unit/test_mcp_cooldown.py tests/e2e/dashboards/test_artifact_conversations.py \
  tests/e2e/test_wait_wake_deprovisioned.py -q --disable-warnings
cd ..
node frontend/tests/unit/artifactChatTimeline.mjs
node frontend/tests/unit/artifactChatLazyLibraries.mjs
node frontend/tests/unit/artifactChatSignIn.mjs
node frontend/tests/unit/artifactPrecompiled.mjs
node frontend/tests/unit/artifactSdkVersion.mjs
```

Baseline `270b629f3`: retaining the new tests while temporarily restoring the original `artifact-globals.js` makes the timeline assertion fail: `Search repository` appears after `Here are the instructions`. Restoring the original wait tool/service makes `test_wait_job_keeps_the_originating_turn_identity` fail with `KeyError: system_completion_id`. Both pass with the fix. The timeline test also re-reads history and verifies label customization; wait integration tests vary app/dashboard surfaces and insert a newer conflicting turn.

Observed verification:

- Broader backend run: **169 passed** (stream, results, wait, timeline, cooldown, conversation HTTP flows).
- Final focused run after added coverage: **33 passed**, including wait wake-up/deprovisioning and redacted errors. Counts overlap the broader run.
- Final stream/redaction rerun: **43 passed**.
- Connector/file-routing checks: **25 passed**.
- Browser component regression run: thinking, composer, scrolling/messages, results, reopened conversations, and composed chat components passed. The original aggregate run's precompile test could not launch because its hard-coded Playwright browser was not installed; adding the same executable override as the other harnesses made the real precompiled/Babel parity test pass.
- New real-browser tests: chronological timeline/history, label override, lazy chart mount/remount (one library load), and sign-in SDK request pass.
- All ten locale catalogs retain their prior key-drift counts. SDK URL/hash pin updated to v6; globals revision is chat-ui-3.

## Loop B — live local published app and ERP

Start a separate sandbox; do not reuse a production database. The built-in `tools/agent/boot_stack.sh` can boot the normal stack. For an existing local frontend, explicitly point `BOW_API_TARGET` at the sandbox backend. When setting `TESTING=true`, set **both** `TEST_DATABASE_URL` and `BOW_DATABASE_URL` to the same isolated database before migrations and server startup (otherwise migrations and the server can use different test files).

```sh
cd backend
.venv/bin/python ../tools/agent/seed_org.py --base-url http://127.0.0.1:8000 > /tmp/chat-seed.json
.venv/bin/python ../tools/agent/seed_chat_app.py --seed /tmp/chat-seed.json
```

The second command prints the new artifact id. It creates a conversation and app via the real API, publishes it, and makes the synthetic app public. No LLM is called. Set the following environment variables using that id and the disposable seeded account, then run:

```sh
export BOW_CHAT_URL=http://127.0.0.1:3100/d/ARTIFACT_ID
export BOW_CHAT_EMAIL=admin@example.com
export BOW_CHAT_PASSWORD='Password123!'
CHAT_VIDEO=1 node frontend/tests/reports/chatPublishedSmoke.mjs
```

Observed PASS: anonymous menu excludes editing, Sign in reaches the real sign-in form and returns to the same app, the owner sees Edit, the ERP launcher opens the iframe with no menu, an anonymous embedded viewer gets a safe host-owned sign-in link, and Hebrew menu direction is RTL. Before/after screenshots and the flow GIF are under `media/pr/embedded-chat-reliability/`. `CHAT_BASELINE=1` captures the original layout when the base component/runtime are checked out. Seeded chat is a UI fixture; without a configured agent/model it correctly reports unavailable after login. Real conversation send/isolation behavior is exercised through HTTP tests with the LLM boundary stubbed.

## Implementation contract

- Wait jobs store the exact originating system-completion id; wake resolves that turn's app context via the existing helper. Current principal membership is still checked. Old jobs without this optional id remain deserializable and retain the restrictive fallback; re-arm them to gain context preservation.
- `timeline` is additive. Legacy `text`, `steps`, `tools`, thinking and results remain. Stream updates replace blocks in place, not append completed tools at the bottom. Titles and bounded tool errors use the same display redaction as history; redaction failure withholds them.
- `ChatMessages`/`ChatMessage` accept `toolLabel(tool)` for presentation only. Default labels prefer the authored title, then the operation and available connection name. The chatbot skill and SDK reference document these contracts.
- Rate-limit failures return retry delay/guidance instead of schema-repair guidance. Worker-local cooldowns are bounded and keyed by organization, principal and connection. Already-in-flight calls and other workers still rely on provider enforcement; no writes are automatically replayed.
- Core libraries still download in parallel. Direct ECharts/EChart consumers and detectable dynamic global access retain eager compatibility; shared result charts can load ECharts later. PDF components can request their runtime when absent. The original precompiled/Babel parity test passes. No external CDN is introduced.
- `bow.auth.signIn()` carries no URL or credential. The trusted host chooses a same-origin return path. Embedded BOW pages expose a separate user-clicked link rather than trying to navigate the customer's ERP or relying on a popup from an asynchronous message callback. Read-only previews refuse runtime operations as before.
- All existing action capability checks remain in the compact menu. Exports remain disabled while busy; run errors surface as a toast. Version controls remain reachable on drafts.

## Boundaries and findings during review

This is not a production deployment or a new Entra/OBO certification. OAuth endpoints, token issuance and identity mapping are unchanged. No live GitHub/provider rate-limit experiment or LLM call-count benchmark was run; the retry guidance and cooldown are deterministic improvements, not a promise of an exact model call count. Cooldowns are deliberately worker-local, not distributed quotas. No claim is made for a particular production load-time percentile; the verified improvement is removing the ECharts download/execution from chat-only startup while retaining later charts.

Review caught and corrected two regressions introduced while consolidating the menu: the Data action needed to become Report when returning, and export actions needed to preserve their busy guard. The final lazy-library regression also caught a case-insensitive `Function` check matching ordinary `function` declarations; splitting case-sensitive JavaScript globals from library-name detection restored deferred loading, and the lazy-chart and precompiled parity tests passed again. Local setup initially pointed migrations and the backend at different test databases; the isolated database variables were corrected before the live pass.

## Follow-up — gate the whole chat app before sign-in

`SharedDashboardView.vue` now resolves viewer identity and identifies apps using the existing `ownConversations` contract before mounting artifact code or fetching visualization data. Anonymous chat apps show a centered app title, subtle lock icon, and one sign-in button. The title comes from metadata already accessible to the viewer; private pages whose metadata is withheld use a generic sign-in heading. Existing translated copy is reused without new catalog keys. Public non-chat artifacts still render anonymously. This is a presentation gate, not a new authorization policy; backend access enforcement remains in place.

The live `chatPublishedSmoke.mjs` regression failed before the change with `Timeout 15000ms exceeded` waiting for `app-sign-in-gate`. It now verifies no artifact iframe/menu exists behind the gate, standalone sign-in returns to the app, anonymous ERP embeds open a separate sign-in tab, and Hebrew RTL renders correctly. The public-dashboard check substitutes only the content response to remove conversation usage, retaining the real metadata/access path. The existing embed/session tests also pass (3 scripts, 15 assertions); configured host SSO/assertion authentication still resolves before this view mounts.

Separate-tab return testing caught a stale in-memory auth token in the original iframe. On returning focus after opening sign-in, the iframe reloads once to read the new cookie; cancelling sign-in returns to the gate without a redirect loop. Cross-site deployments that block third-party cookies must use the existing configured host authentication; this test uses a local mock ERP. Screenshots: `gate-before.png`, `gate-after.png`, `gate-embedded.png`, `gate-he.png`; flow: `gate-flow.gif`.

## Follow-up — one-click app SSO

The app gate now sends an explicit `app_sso=1` intent to `/users/sign-in` with the app return path. The sign-in page consumes the marker from browser history before invoking the existing provider helper. Exactly one enabled SSO provider starts automatically for this explicit app flow (SSO-only or hybrid); local-only mode, LDAP, multiple providers, `local=true`, error callbacks and ordinary sign-in visits retain the existing choice/form. The existing OAuth consent auto-start remains supported. No new provider or token-exchange code is introduced.

Run `PW_CHROMIUM_PATH=... node frontend/tests/reports/appSsoSignIn.mjs` against the local stack (`BOW_BASE_URL` defaults to port 3100). Before the change the assertion failed with **0 !== 1** provider requests. After the change it passes single-provider handoff, multiple-provider choice, hybrid/local-only modes, error/local overrides, ordinary sign-in, refresh after failure, and persisted app return path on provider navigation. Settings and provider responses are stubbed; this proves browser routing and loop guards, not live Entra authentication. The existing published-app test also verifies local sign-in and ERP return. Screenshots and the SSO flow GIF use a clearly labeled test IdP.
