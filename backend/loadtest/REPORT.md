# Performance & reliability under concurrent load

**Question:** how blocking is the main flow — `completion_service` → `agent_v2` —
and how many parallel users can we serve when some are running the agent and
others are just using the app (`/agents`, adding and editing instructions)?

**Answer in one line:** the agent flow itself never refused a request, but at
100 concurrent users **fewer than half the runs actually completed** — and the
client could not tell, because the SSE stream closes cleanly either way.

Method, sandbox shape, and the guards against measuring our own harness are in
[`METHOD.md`](METHOD.md). Raw data in `results/`.

---

## 1. Capacity summary

Three cohorts run simultaneously at each level: ~60% driving the agent flow over
streaming SSE, ~40% doing `/agents` + instruction CRUD, plus 4 real Chromium
browsers doing both.

| | 30 users | 60 users | 100 users |
|---|---|---|---|
| Agent runs — client saw success | 100% | 100% | 100% |
| Agent runs — **actually succeeded** | **100%** | **94.4%** | **43.3%** |
| Agent p50 / p95 latency | 70.7s / 105.3s | 78.8s / 131.9s | 91.0s / 128.0s |
| Ordinary app calls failed | 12 / 190 (6%) | 82 / 666 (12%) | **351 / 1319 (27%)** |
| `create_instruction` p95 | 46.7s | 32.8s | 38.8s |
| Postgres connections (97 usable) | 70 | **100** | **100** |
| Connection refusals ("too many clients") | 0 | 19 | **555** |
| Browser chat turn p95 | 20.6s | **132.5s** | 89.1s |

Single-user reference: one agent run alone, same box, same prompt: **9.9s**.

**Verdict.** 30 concurrent users is serviceable but already unpleasant —
instruction writes take tens of seconds. **60 is where the database ceiling is
first hit** (connections exhausted, first silent data loss, browser chat turns
over two minutes). **100 is well past the failure boundary**: more than half the
agent runs never completed, and a quarter of ordinary app requests errored.

The practical safe number on this hardware and configuration is **around 30**,
and that is limited by instruction writes, not by the agent flow (§6).

---

## 2. The most important finding: success is reported that did not happen

At L100 the harness — like a browser — saw all 60 streams finish normally. The
`completions` table disagreed:

| level | client says finished | db: success | db: error | db: in_progress (stuck) | real success |
|---|---|---|---|---|---|
| L30 | 18/18 | 18 | — | — | 100.0% |
| L60 | 36/36 | 34 | 2 | — | 94.4% |
| L100 | 60/60 | 26 | 11 | **23** | **43.3%** |

Two distinct defects produce this.

### 2a. The SSE stream terminates identically on success and failure

The stream ends with `[DONE]` whether the agent finished, errored, or never
reached a terminal state. Any client that treats "stream closed" as "run
succeeded" — which is the natural reading — will over-report success. This is
why the earlier `FINDINGS.md` "network error" symptom appears fixed while the
failure mode has actually *moved*: it is no longer a visible error, it is a
silent one.

### 2b. Error recovery runs on the connection that just died

`completion_service.py:2350` marks a failed completion as `error` using **the
same session** whose connection just failed, and swallows the result:

```python
try:
    await session.execute(update(Completion)...values(status='error', ...))
    await session.commit()
except Exception:
    pass
```

When the failure *is* connection exhaustion, that update cannot succeed either,
so the row stays `in_progress` forever — 23 of them at L100. In the UI that is a
spinner that never resolves. Our Playwright driver hit exactly this and sat on a
hung turn until its 300s deadline.

**The fix already exists in this file.** The background path
(`completion_service.py:795`) opens a *fresh* session for recovery, with a
comment explaining precisely this hazard:

> Mark the completion as errored on a fresh session — the current one may be
> poisoned […] leaving the row stuck in 'in_progress' forever.

The streaming path needs the same treatment.

---

## 3. What actually breaks first: Postgres connections

The 11 errored completions all carry the same message:

```
Agent failed: sorry, too many clients already
```

Postgres refused new connections. The arithmetic is not subtle:

| | |
|---|---|
| Async pool per worker | `pool_size=20 + max_overflow=20` = 40 (`database.py:256`) |
| Workers in this sandbox | 2 → **80** |
| Plus sync scheduler engine, monitoring, etc. | ~10–17 |
| `max_connections` | 100, minus 3 superuser-reserved = **97 usable** |

We measured 97 at L100 — the ceiling, exactly. During that window `psql` itself
could not connect, so an operator would be locked out of the database mid-incident.

**This is worse in production than in this sandbox.** `start.sh` sets
`workers = min(4, CPUs/2)`, so a 8-vCPU production box runs **4 workers × 40 =
160 connections** against a stock `max_connections=100`. Production reaches this
wall at roughly *half* the concurrency measured here.

Notably `QueuePool` timeouts were **zero** at every level. The pool-side
mitigations from the previous investigation (`_release_db_between_steps`, the
agent semaphore) are working; the ceiling has simply moved one layer down, from
the application pool to the database itself.

---

## 4. Where the agent's time actually goes

Phase tracing (`app/core/phase_trace.py`, env-gated) splits each run into
queueing, pool/setup, and real execution:

| level | queue wait p50 | queue wait p95 | pool+setup p95 | agent exec p50 | agent exec p95 | LLM calls/run |
|---|---|---|---|---|---|---|
| L30 | 10ms | 59,366ms | 8,643ms | 33s | 63s | 3.5 |
| L60 | 21ms | 63,555ms | 8,290ms | 53s | 78s | 3.5 |
| L100 | **56,397ms** | **82,250ms** | 10,630ms | 31s | 57s | 2.0 |

Two things to read here.

**The actual agent loop does not get slower.** `agent exec` p50 is 33s / 53s /
31s — it does not grow with concurrency. The work per run is also constant
(3.5 LLM calls at L30 and L60, by construction and verified). At L100 it drops
to 2.0 only because a third of the runs died partway through (§2).

**Queueing is the entire story at L100.** At L30 and L60 the *median* run barely
queues (10–21ms) while the tail waits a minute — a bimodal queue. At L100 the
median itself waits **56 seconds**, i.e. of a 91s p50 end-to-end, roughly 60%
is spent waiting for an agent slot before any work begins.

The bimodality at L30/L60 has a specific cause: worker imbalance.

### Per-worker semaphores + skewed connection distribution

`_AGENT_RUN_SEMAPHORE` (`completion_service.py:65`) caps concurrent agent runs at
12 **per worker**, so 2 workers should allow 24. Uvicorn's distribution of
long-lived SSE connections is badly uneven:

| level | completions per worker | waited > 1s for a slot |
|---|---|---|
| L30 | 4 / 14 | 2 of 18 |
| L60 | 22 / 14 | 12 of 36 |
| L100 | **46 / 14** | **36 of 60** |

At L100 one worker took 46 of 60 streams while the other idled at 14. The
nominal capacity of 24 was never reached; the busy worker queued at 12 while its
peer had spare slots. Adding workers does not fix this cleanly — it widens the
imbalance window. A shared (cross-worker) limit, or connection-count-aware
balancing, is the structural answer.

---

## 5. Interference: the agent flow and ordinary app use

Quiet baseline (browsing cohort alone, no agents): p50 71ms, p95 447ms, 0 failures.

| level | calls | failed | p50 | p95 | p95 vs baseline |
|---|---|---|---|---|---|
| L30 | 190 | 12 | 1,671ms | 22,910ms | **51×** |
| L60 | 666 | 82 | 1,664ms | 15,107ms | **34×** |
| L100 | 1,319 | 351 | 462ms | 16,917ms | **38×** |

Broken down by operation (p95, ms) the picture is sharp — **reads survive,
writes collapse**:

| level | list_agents | list_instructions | **create_instruction** | **edit_instruction** | list_reports |
|---|---|---|---|---|---|
| baseline | 50 | 128 | 421 | 557 | 85 |
| L30 | 2,154 | 2,855 | **46,659** | **20,557** | 2,138 |
| L60 | 3,345 | 4,281 | **32,842** | **17,869** | 3,548 |
| L100 | 5,909 | 3,934 | **38,805** | **28,453** | 1,548 |

Listing agents and instructions degrades to seconds but essentially never fails.
Creating or editing an instruction — the exact workload described — reaches
**30–47 second p95** and produces most of the failures.

### Why instruction writes are the weak point

`pg_stat_statements` during the ramp is unambiguous. The top queries by total
database time are *all* on `instruction_builds`:

| query | total time | calls | mean |
|---|---|---|---|
| `SELECT instruction_builds.id WHERE organization_id=$1 AND is_main FOR UPDATE` | **2,769,723ms** | 1,518 | **1,825ms** |
| `UPDATE instruction_builds SET is_main=…` | 671,454ms | 1,466 | 458ms |
| FK row-lock check on `instruction_builds` | 301,746ms | 40,093 | 7.5ms |

That first query runs in **0.05ms** uncontended (verified with
`EXPLAIN ANALYZE`; it uses `ix_instruction_builds_pending_sweep`). Averaging
1,825ms means ~36,000× amplification from **pure lock waiting**, not a bad plan.

The cause is `_claim_main` (`build_service.py:911`), which is *deliberately*
serialized. Its docstring explains the invariant it protects: exactly one
`is_main` build per org, because two racing promotions previously corrupted the
whole instruction surface. The `SELECT … FOR UPDATE` is the mechanism.

**This is a correctness/concurrency trade-off, not a bug.** But the lock is
taken *inside* the larger instruction-write transaction, so it is held while
that transaction does its other work, and every instruction write in the
organization queues behind it. At 40 concurrent browsing users this is the
app's write-throughput ceiling.

Two secondary consequences:

- **`edit_instruction` returns 500 where `create_instruction` returns 503.**
  Create has a graceful degradation path (`instruction_service.py:219`) that
  soft-deletes the orphan and asks the client to retry. Update has no
  equivalent: concurrent writes surface as an unhandled `IntegrityError` on
  `uq_build_content_build_instruction`, raised from the read-then-insert in
  `add_to_build` (`build_service.py:609-629`), which then poisons the session
  (`PendingRollbackError` cascades).
- **The graceful path catches the wrong failure.** Its comment anticipates a
  `lock_timeout` from a long-running transaction. We recorded **zero**
  `lock_timeout` events; the actual triggers were `DeadlockDetectedError` and
  autoflush `IntegrityError`.

---

## 6. Which cohort is actually causing the damage?

The ramp shows the agent flow slowing *and* browsing collapsing at the same
time, which cannot by itself say which causes which. So the same agent
concurrency (36) was run twice: once alone, once with the browsing cohort.

| arm | agents | browsers | agent real success | agent p50 | agent p95 | browse p95 | browse failures |
|---|---|---|---|---|---|---|---|
| **agents only** | 36 | 0 | 100% (36/36) | **41.6s** | **60.1s** | — | — |
| **agents + browsing** | 36 | 24 | 100% (36/36) | 49.7s | 82.7s | 7,687ms | 106 |

Top queries by total database time, same 36-agent load in both arms:

| arm | hottest query | total | per call |
|---|---|---|---|
| agents only | `SELECT instructions.category …` | **515ms** | 2.5ms |
| agents + browsing | `SELECT instruction_builds.id … FOR UPDATE` | **837,470ms** | **1,837ms** |

Three conclusions, stated carefully because they differ in strength:

1. **The agent flow contributes almost no database load.** Across the whole
   agents-only arm the busiest query totalled 515ms and the top four together
   came to about 1.2 seconds. Nothing touched `instruction_builds`.
2. **Browsing traffic contributes essentially all of it.** The same agent load
   plus 24 browsing users produced 837 *seconds* on one lock-bound query. This
   is a difference of nearly six orders of magnitude, from the cohort that looks
   like the cheap one.
3. **At this level that costs the agent flow ~19% at p50 and ~38% at p95** —
   real, but not catastrophic, because an agent run spends most of its life
   waiting on the LLM rather than on the database. Both arms still completed
   100% of runs.

So "how blocking is the main flow?" has a two-part answer. **The agent flow is
not blocking itself** — 36 concurrent runs alone are healthy and cheap. What
breaks at 60 and 100 is that instruction writes saturate the *shared* connection
pool and database, and the agent flow, which needs a connection between every
LLM step, is what visibly dies when that runs out (§3).

That inverts the intuitive priority: raising `BOW_MAX_CONCURRENT_AGENTS` or
tuning the agent loop would not have helped. Shortening the instruction-write
critical section (§5) should help both cohorts at once.

## 7. What real browsers saw

Four Chromium contexts ran throughout, driving login, `/agents`,
`/instructions`, and complete chat turns.

| | login p95 | /agents p95 | /instructions p95 | **chat turn p95** | failures | "network error" |
|---|---|---|---|---|---|---|
| baseline | 5,730ms | 1,770ms | 2,093ms | 11,860ms | 0 | 0 |
| L30 | 5,311ms | 5,332ms | 5,252ms | 20,569ms | 0 | 0 |
| L60 | 6,555ms | 17,652ms | 9,507ms | **132,503ms** | 0 | 0 |
| L100 | 4,886ms | 7,484ms | 6,925ms | **89,082ms** | 2 | 0 |

A chat turn that takes 12s on an idle system takes **2 minutes 12 seconds** at
L60. Page loads degrade to seconds but keep working.

**No browser ever displayed a "network error" toast** at any level — the symptom
that motivated the original investigation is genuinely gone, and the pool fixes
from `FINDINGS.md` hold. What replaced it is harder to diagnose: a chat turn
that renders as permanently "Thinking", which is the UI face of the stuck
`in_progress` rows in §2. Our own Playwright driver was caught by this — it sat
on a hung turn until its 300s deadline, well past the 150s the run allotted it.

Caveat: production serves the SPA from the *same* uvicorn process
(`SERVE_FRONTEND=1`); this sandbox runs Nuxt separately, so these UI numbers are
if anything optimistic.

---

## 8. Resource ceilings

| level | backend CPU p95 | backend CPU max | backend RSS | PG conns (97 usable) | idle-in-txn | PG lock waits | QueuePool timeouts | "too many clients" |
|---|---|---|---|---|---|---|---|---|
| L30 | 158% | 176% | 1,512MB | 70 | 37 | 20 | 0 | 0 |
| L60 | 179% | 226% | 1,664MB | **100** | 66 | 30 | 0 | 19 |
| L100 | 157% | 182% | 1,802MB | **100** | 61 | **52** | 0 | **555** |

CPU is a per-core sum, so 400% is the whole box. The backend peaked at ~226%,
i.e. **a little over half the available CPU — it was never CPU-bound**. Memory
was never a constraint either.

What climbs monotonically is lock waiting (20 → 30 → 52 concurrent sessions
blocked on locks) and connection refusals (0 → 19 → 555). That is the real
scaling wall, and it is consistent with §3 and §5.

Getting this right took two corrections worth recording, since both would have
produced confidently wrong conclusions: the original sampler matched no backend
process at all (`ps -C uvicorn`), and the replacement over-read CPU past 400% of
a 4-vCPU box because it assumed a 1s sampling tick while its own `psql` call
stretched the interval under load. Both are described in `METHOD.md`.

One measurement caveat worth stating: this run had `DEBUG` logging active, which
is the default whenever `ENVIRONMENT != "production"`
(`logging_config.py:40`) and causes the OpenAI client to write every full prompt
to disk. `start.sh` sets `ENVIRONMENT=production`, so shipped deployments are
unaffected — but anyone running `uvicorn` directly is paying an unmeasured tax
and writing prompt content to logs.

---

## 9. Recommendations, in the order I would do them

| # | Change | Why | Effort |
|---|---|---|---|
| 1 | Recover failed completions on a **fresh session** in the streaming path, mirroring `completion_service.py:795` | Eliminates permanently-stuck `in_progress` rows — the worst failure because it is invisible | Very low |
| 2 | Emit a terminal SSE event that distinguishes success from failure, and have clients trust *that* rather than stream closure | Makes failures visible to the user and to monitoring | Low |
| 3 | Document and enforce `max_connections ≥ workers × (pool_size + max_overflow) + headroom`; fail loudly at startup if it is not | 4 workers × 40 = 160 vs a stock 100 is a guaranteed outage under load | Low |
| 4 | Take the `_claim_main` lock in its own short transaction rather than inside the instruction-write transaction | Cuts the serialized critical section from seconds to milliseconds; addresses the single biggest source of DB time | Medium |
| 5 | Give `update_instruction` the same graceful-retry path `create_instruction` has, and make `add_to_build` insert idempotently (`ON CONFLICT DO NOTHING`) | Turns 500s into retryable outcomes | Low |
| 6 | Replace the per-worker agent semaphore with a shared limit (Redis/advisory lock), or reduce worker count and raise the per-worker cap | The nominal cap of `workers × 12` is never actually reached | Medium |
| 7 | Run the app as a non-superuser Postgres role | So `superuser_reserved_connections` actually reserves operator headroom (our sandbox role was superuser, which is why `psql` was locked out too) | Very low |

With 1, 3 and 5 alone, the L100 run would have reported honest results and lost
far fewer completions. With 4, the interference numbers in §5 should improve
substantially — that is the change I would most want to measure again.

---

## 10. Limits of this study

- **Single organization.** The `instruction_builds` lock is per-org, so its
  contention is worst-case here. Many orgs would spread this out.
- **One box.** App, database, mock LLM, frontend and load generator share 4
  vCPU. Backend CPU is attributed separately (§7), but they do compete.
- **Mock LLM for the scale runs**, with a real-Haiku validation pass at lower
  levels. Rationale in `METHOD.md`.
- **Not tested:** per-report prompt queueing (each user had their own report),
  multi-org contention, file uploads, MCP tools, `create_data` (which spawns
  the nested coder agent and would raise DB load per run substantially).
- The agent-flow work per run was held constant by construction and verified
  (3.5 LLM calls/run at every level), so latency differences are contention.
