# How these numbers were produced

Companion to `REPORT.md`. Everything here is reproducible from this directory.

## The sandbox

| | |
|---|---|
| Host | 4 vCPU / 15 GB, single box |
| Database | PostgreSQL 16, `max_connections = 100` (stock default) |
| Backend | uvicorn, **2 workers**, no `--reload` |
| Frontend | Nuxt **production build** (`yarn build` + `node .output/server/index.mjs`) |
| LLM | local mock (see below), with a real-Haiku validation pass |
| Data | chinook demo data source, 100 seeded users in one org |

Two workers is deliberate. `start.sh` — the real production entrypoint — sets
`workers = min(4, CPUs/2)`, so 2 workers on 4 vCPU is exactly what this box
would run in production. It is also the same shape as the earlier
`FINDINGS.md` investigation, so the numbers are comparable to that baseline.

**The frontend must be a production build.** A `yarn dev` server compiles
routes on first request; the first page load measured 48 s, which would have
swamped every UI number with dev-server compile time rather than app latency.

## Why a mock LLM

At 60–100 concurrent completions, real provider calls contribute rate-limit
429s and multi-second latency variance that dominate the measurement — you end
up measuring Anthropic, not BoW. `mock_llm.py` is an OpenAI-compatible server
wired in as a `custom` provider (`additional_config.base_url`), with a
deliberate latency shape (~600 ms to first token, then ~60 tokens at ~12 ms
each). Every level therefore sees an identical LLM and the only variable is
BoW's own contention.

The mock drives a realistic planner loop: two `create_note` tool calls, then a
final text turn. `create_note` was chosen because it exercises the real
tool-execution, completion-block, streaming-event and DB-write paths while
spawning no nested sub-agent — which keeps the LLM call count per completion
deterministic, so a slowdown at L100 is contention rather than extra work.

`mock_L*.jsonl` records LLM calls per level so the report can *show* that work
per completion stayed constant rather than assuming it.

## The three cohorts

Run simultaneously, because the question is not "how fast is the agent alone"
but "what does the agent flow do to everyone else":

- **A — agent users** (60%): `POST /completions` with `stream=true`, the full
  `agent_v2` loop, measured for client-visible SSE reliability and latency.
- **B — browsing users** (40%): the `/agents` page and instruction CRUD —
  list agents, list instructions, create an instruction, edit it, list reports.
  Read **and** write, so they contend for the same tables and pool.
- **C — real browsers** (4 Chromium contexts): Playwright driving the actual
  UI — login, `/agents`, `/instructions`, and full chat turns.

Cohort C is small on purpose: each context is a real renderer process and this
box has 4 vCPU. It is a fidelity probe, not the load source.

## Guarding against measuring ourselves

A load test that saturates its own client produces numbers about the client.
Three guards:

1. The harness samples its own asyncio event-loop lag. It stayed at ~2–3 ms
   p95 at every level, so the client was never the constraint.
2. The mock LLM reports peak in-flight requests and counts its own scheduling
   overruns.
3. `sample_metrics2.sh` attributes CPU **per role** (backend / mock / harness /
   Postgres / frontend) by diffing `/proc/<pid>/stat` jiffies.

Guard 3 exists because it caught a real error. The original `sample_metrics.sh`
used `ps -C uvicorn`, which matches nothing here — the workers are `uv
run`-spawned multiprocessing children whose `comm` is `python`. That left
host-wide CPU as the only number, and host CPU on this box also includes the
harness, the mock LLM, the frontend and Postgres. Reporting it as "the app is
CPU-bound" would have been wrong. The first-pass ramp (`results/mock/`) carries
the uncorrected column; `results/full/` has the per-role one.

`ps`'s own `%cpu` is a lifetime average and was also wrong for this purpose —
it read the backend at 26% of the box during a period when jiffy-diffing showed
it much higher.

The jiffy-diffing version then had a bug of its own: it converted jiffies to a
percentage assuming a 1 s tick, but its loop also runs a `psql` query that slows
down under load, so the real interval stretched past 1 s and CPU over-read —
producing 507% on a 4-vCPU box, which is impossible. `analyze.py` rescales by
the actual wall time between samples, which the `ts` column records. The
corrected peak is ~226%, i.e. the backend was never CPU-bound.

Both mistakes are recorded rather than quietly fixed because each one, taken at
face value, would have supported a confident and wrong conclusion about whether
this workload is CPU-limited.

## Client-visible success is not success

The harness originally scored a run as successful when its SSE stream ended
with `[DONE]`. That is what a browser sees, and it is what the earlier
investigation used. It is not the truth: the stream terminates cleanly even
when the agent run failed or never reached a terminal state.

`verify_server_side()` therefore re-reads the `completions` table after each
level and reports the real status distribution. The gap between the two is one
of this report's main findings.

## Instrumentation added to the app

`app/core/phase_trace.py` — env-gated (`BOW_PHASE_TRACE`) JSONL tracing, a
single module-level boolean check when disabled. Wired into the streaming
completion path to separate:

- **queue wait** — request accepted → agent semaphore acquired
- **pool + setup** — semaphore acquired → first DB objects loaded
- **agent execution** — the `agent_v2.main_execution()` span

This is what lets the report say *"at L100 most of the wait was queueing"*
instead of just *"it got slower"*.

## Deliberate test-fixture deviations

Stated so the numbers can be read correctly:

- **Signups enabled** (`loadtest/bow-config.loadtest.yaml`) to provision 100
  users. Gates registration only; no effect on the flows under test.
- **`manage_instructions` + `create_data_source` granted to the `member`
  role.** The workload under test has ordinary users authoring instructions
  from `/agents`; the stock `member` role has neither permission and the
  role-editor API is enterprise-gated. Without this, cohort B would have
  measured 403 handling. This models an org whose members are analysts.
- **`BOW_ENCRYPTION_KEY` pinned.** Running `uvicorn --workers N` directly
  without it gives each worker its own generated key, so JWTs minted by one
  worker are rejected by the others. `start.sh` already handles this correctly
  (generates and exports before forking, with a warning); this only bites when
  bypassing it, as the sandbox does.
- **License seat cap of 100** limited the org to 99 members + admin, which is
  exactly L100.

## Reproducing

```bash
cd backend
bash loadtest/ctl.sh start                 # postgres + mock llm + backend
uv run python loadtest/seed.py --users 110 # users, org, mock provider, chinook
bash loadtest/run_full.sh "30,60,100" full # 3-cohort ramp
bash loadtest/run_experiments.sh           # isolate / semaphore / logging
python loadtest/analyze.py loadtest/results/full
```

Layout: `ctl.sh` (process control), `seed.py`, `mock_llm.py`, `harness_v2.py`
(cohorts A+B), `ui/ui_driver.js` (cohort C), `sample_metrics2.sh`,
`analyze.py`, `exp_isolate.py`, `exp_semaphore_bypass.py`.
