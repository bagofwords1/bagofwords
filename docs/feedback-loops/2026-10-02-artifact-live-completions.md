# Artifact apps through real completion flows

On 2026-10-02, the user approved real Luna and Sol calls through the full
completion flow. These runs use ordinary prompts, normal planner/tool selection,
real artifact creation and real model-backed runtime streaming. No expected
schema, supplied app source or scenario recipe is included in the user prompts.

## Results and limits

The six-case API suite passed in 484.47 seconds: notes, document summaries, and
blog creation plus a permissions follow-up, each on `gpt-6-luna` and `gpt-6-sol`.
The document cases also called their generated AI operation against the real
provider. Blog assertions cover declared public published-only reads and denied
public mutations; they do not independently prove the entire generated blog UI.

Four additional normal completions created persistent sandbox apps: document
summaries and a fresh reading-list request on each model. Their report/version
IDs, exact prompts, source hashes, tool outcomes and actual model usage are in
`media/pr/artifact-resources/live-evaluation.json`.

| Browser journey | Luna | Sol |
| --- | --- | --- |
| Actual generated document UI: upload, real stream, save, reload | Passed | Passed |
| Text grows while generation is still active | Passed | Passed |
| First reading-list UI attempt: save a new article | Failed: platform blocked forms | Failed: platform blocked forms |
| Same generated reading-list source after platform fix: save, mark done, filter, reload | Passed | Passed |

The document UI was exercised twice with real model output. The second pass used
unique filenames and a passive DOM observer, avoiding false conclusions when a
model finished before the screenshot. Both recorded multiple text lengths while
streaming remained active. Record persistence was checked by reloading the page.
Generated source was not edited by a human. Luna's document run performed an
ordinary automatic edit during its completion; it was not a stronger-model rescue.

This is bounded live verification, not the plan's full repeated release matrix.
The reading-list case became a regression after exposing the form bug; it is no
longer independent held-out evidence for subsequent changes. Three independent
attempts per core journey and deployment capacity/failover qualification remain.

## Root cause reproduced and fixed

The generated reading apps used standard `<form onSubmit={...}>` controls. All
three live artifact iframe surfaces omitted `allow-forms` from their sandbox.
Chromium blocked native submission before the JavaScript handler ran, so neither
model's Save button reached the records API. Earlier hand-authored fixtures used
button click handlers and did not expose this compatibility gap.

The iframe sandbox in `frontend/components/dashboard/ArtifactFrame.vue:422` (and fullscreen at 527) and
`frontend/pages/r/[id]/index.vue:216` now permits JavaScript form handlers. The page
content-security policy already denies `form-action`; the legacy slide document
now also explicitly denies it (`frontend/utils/artifactIframe.ts:133`; page policy at 337). Same-origin privilege remains absent. This permits
local submit handlers without permitting form navigation to an external server.

`frontend/tests/unit/artifactFormIsolation.mjs` exercises the actual HTML builder
and sandbox policies in Chromium. Before the fix it timed out waiting for the
local submit handler. After the fix it passes for page and slide documents and
observes the CSP rejection of an attempted external form submission, with zero
requests reaching that destination. The same two generated reading apps then
passed save/update/filter/reload without regeneration or source edits.

## Other findings, not hidden successes

- The first persistent harness selected model names from an old controlled
  provider's catalog. Both requests failed immediately with provider 404s. The
  harness now selects a dedicated real provider by its identity. Those two
  setup failures are excluded from model outcomes and retained in the evidence.
- The initial persistent document runs attempted their own browser verification
  at `localhost:3000`, while the sandbox frontend was on 3118. Their completion
  messages correctly disclosed unavailable interaction verification. Independent
  live UI checks passed. Setting `BOW_ARTIFACT_PREVIEW_URL` to the actual frontend
  fixed preview navigation for the fresh reading-list runs; Sol also performed
  successful browser actions inside the normal completion flow.
- The first reading browser harness used an immediate checkbox-state assertion
  even though the generated app waits for the record API before updating state.
  It now clicks and waits for the returned state before asserting persistence.
- The API suite emitted three SQLite worker-thread `Event loop is closed` warnings
  during teardown, as well as deprecation warnings. They are recorded, not
  represented as clean teardown. All six assertions passed; the persistent
  server/browser checks do not tear down the event loop between each request.

The final frontend production build, existing SDK/data compatibility checks and
the new form-isolation browser regression passed after the fix. The active
evaluation credential was replaced with a non-secret placeholder in the disposable
sandbox provider after all real calls completed; no key is committed in the repo.
A fresh valid credential is needed to make further model calls from these apps.

## Reproduction

Use a disposable database and the normal synthetic sandbox account from the main
artifact feedback-loop report. Start the API with the feature enabled, stable
sandbox encryption key and shared file directory. Set
`BOW_ARTIFACT_PREVIEW_URL=http://127.0.0.1:3118` and start the frontend there.
Credentials must be provided through environment variables, never command text
or committed files. Logging must redact the test credential.

```sh
# From backend, with OPENAI_API_KEY_TEST already set securely:
ARTIFACT_LIVE_EVAL_APPROVED=true TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db .venv/bin/python -m pytest tests/ai/test_artifact_resources_live.py -v --tb=short

# From repository root, with the running sandbox:
ARTIFACT_LIVE_EVAL_APPROVED=true backend/.venv/bin/python tools/agent/artifact_resources/live_completion.py
node tools/agent/artifact_resources/inspect_live.mjs
node tools/agent/artifact_resources/live_documents.mjs

# Reuse only the dedicated real provider's ID; this is not a credential.
ARTIFACT_LIVE_EVAL_APPROVED=true ARTIFACT_LIVE_CASES=reading ARTIFACT_LIVE_OUTPUT=/tmp/artifact-live-reading.json backend/.venv/bin/python tools/agent/artifact_resources/live_completion.py
node tools/agent/artifact_resources/live_reading.mjs
node frontend/tests/unit/artifactFormIsolation.mjs
```

The browser scripts follow controls observed in these actual generated versions;
new generations can choose different labels/layouts and require fresh UI
inspection. These selectors are never supplied to the model. The optional
`ARTIFACT_LIVE_EVIDENCE` directory saves synthetic API-suite outputs. The persistent
runner records failures and exits unsuccessfully if a completion fails.

Screenshots: `live-reading-before.png`, `live-gpt-6-luna-reading.png`,
`live-gpt-6-sol-reading.png`, `live-gpt-6-luna-stream.png`,
`live-gpt-6-sol-stream.png`, `live-gpt-6-luna-saved.png`,
`live-gpt-6-sol-saved.png`, under `media/pr/artifact-resources/`.
