# Dashboard version polling in a report

Open a report with a completed v7 dashboard and click its counter. With the original version poll from PR #1841, each unchanged response replaced the artifact list. The iframe HTML depended on that list and embedded a new runtime nonce on every rebuild. Chromium therefore navigated the iframe and reset its counter. When v8 arrived, the pane offered a manual refresh.

## Reproduce

Start the Nuxt frontend on localhost. The probe uses Playwright from the frontend dependencies, a local test session, and fixed API responses. It renders a real interactive dashboard in the report pane. No backend or production account is required.

```sh
cd frontend
yarn dev --host 127.0.0.1 --port 3318
```

From the repository root in another terminal, run the probe with the frontend's local vendor libraries available (see `scripts/download-vendor-libs.sh`).

```sh
node docs/feedback-loops/dashboard-version-refresh/probe.cjs
```

On a DevBox, use the provisioned frontend with `ORIGIN=http://localhost:3000`. The default run waits for three 15-second polls. Set `FOCUS=1` to trigger the same poll on window focus. To record the old behavior, run the probe against the pre-fix frontend with `BASELINE=1 FOCUS=1`.

## Browser observations

The initial document load is excluded. Each row records the visible heading, counter, and cumulative iframe load events after the poll.

| Poll response | Before | After |
| --- | --- | --- |
| v7 again | v7, counter 0, 1 load | v7, counter 1, 0 loads |
| v8 arrives | v7, counter 0, 2 loads | v8, counter 0, 1 load |
| v8 again | v7, counter 0, 3 loads | v8, counter 1, 1 load |

The probe clicks the counter again after the automatic switch. It also checks that an unrelated dashboard, a pending v9, and a repeated completion event for v8 preserve the counter and load count.
