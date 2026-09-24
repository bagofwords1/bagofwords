# Feedback loop — one warm refresh per tool batch, slim snapshots, overlapped browser policy

**PR-C of the agent_v2 performance stack** (items 3, 4 and 11 of the plan).

After each tool, the agent ran a warm context refresh
(`post_tool_before_block_update`) under `_tool_db_lock`. Its view fed only two
things:
- the token meter;
- a `post_tool` context snapshot.

A batch of N parallel tools therefore ran N extra refreshes, serialized on the
lock, right before the post-batch refresh (`post_tool_next_iteration`) built
the same view again. Context snapshots also `model_dump`ed the full
schemas/instructions sections only to throw them away. The browser-tool policy
check ran serially before every loop-start refresh.

## Change

- **Per-tool post refresh removed.** The token meter and **one** `post_tool`
  snapshot per batch now use the post-batch refresh that already ran. The
  snapshot id is back-filled onto every tool execution in the batch with one
  Core UPDATE.
- These refreshes are unchanged:
  - the pre-tool refresh, which defines what each tool sees;
  - the loop-start refresh, which picks up background writes.
- **Slim snapshots.** `_build_slim_context_snapshot` computes the usage
  summaries first, then dumps with `exclude={"static": …}`. The output was
  verified equal to the old builder on the real schema context (6 cases).
  `save_context_snapshot` no longer refreshes (re-reads) the JSON it just
  wrote.
- **Browser-tool policy** (its own session) starts before the loop-start
  refresh and is awaited where it ran before, so the two overlap.

### Intended behavior changes (visible in the trace / Context Browser)

| | before | after |
|---|---|---|
| `tool_executions.context_snapshot_id` | **NULL** for every tool: the per-tool snapshot was never linked | set to the batch's `post_tool` snapshot |
| `post_tool` snapshot contents | taken before the tool's observation landed: `warm.observations` **empty** (~17k chars) | includes the batch's observations (~42k chars) |
| `post_tool` snapshots per batch | N (one per tool) | 1 |

## Loop — real UI scenarios (before = PR-B, after = PR-B + PR-C)

The harness, gates and scenarios are the same as in
`agent-persistence-single-commit.md`.

| env · scenario | metric | before | after | Δ |
|---|---|---|---|---|
| P0 · every S1/S3 run | warm refreshes / run | 9 | **6** | −3 |
| | context snapshots saved / run | 6 | **4** | −2 |
| P0 · S1 ×3 | whole run | 39.9s | **32.5s** | −19% |
| | parallel tool batch | 17.9s | **11.6s** | −35% |
| | SQL / run | 1239 | 1134 | −8% |
| P0 · S3 ×3 | whole run · batch | 33.3 · 12.3s | 33.5 · 11.9s | ≈ |
| | `_refresh_warm_traced` total | 636 ms | 447 ms | −30% |
| P0 · S2 ×2 | whole run | 10.8s | 10.2s | |
| P1 · S1 ×2 | whole run | 66.5s | **56.7s** | −15% |
| | parallel tool batch | 31.3s | **21.2s** | −32% |
| | refresh time / run | 6199 ms | 4420 ms | −29% |
| P1 · S3 ×2 | whole run · batch | 53.6 · 20.2s | 56.1 · 22.3s | +5% (see below) |
| SQLite · S3 ×3 | tool batch | 10.5s | 10.6s | = |
| | whole run | 28.4s | 33.0s | see below |
| SQLite · S6 | steer applied · Stop | 1026 · 1255 ms | 907 · 1449 ms | |

**Why P1 S3 and SQLite S3 are slower: model variance, not this change.**

On SQLite:
- the tool batch is identical (10.5 vs 10.6s);
- every non-LLM method is equal or faster (the refresh total is 461 → 382 ms);
- the extra time is in the planner's LLM stream: 6.8s vs 8.4–12.2s for the
  first plan, and the final answer took about 2s longer;
- PR-C's runs used 2031–2528 completion tokens against 1551–1855 for PR-B,
  and about 1,000 more prompt tokens.

The prompt diff shows where the extra tokens came from. The model sometimes
puts `data_source_id` in `tables_by_source`. When it does, the existing
focus-on-use hook sets `report.focused_data_source_ids`, and the next planner
call renders an `<available_agents>` roster. That hook fired at random in every
phase, including the baseline:

| phase | S1/S3 runs with focus set |
|---|---|
| base P0 | 2/6 |
| PR-B P0 | 5/6 |
| PR-C P0 | 2/6 |
| PR-C P1 | 4/4 |
| PR-C SQLite | 2/3 |
| PR-B SQLite | 0/3 |

## Correctness

| env | runs passing all gates |
|---|---|
| P0 | 12/12 |
| P1 | 4/4 |
| SQLite | 5/7 |

Across all of these runs:
- 94 event→fetch checks, 0 mismatches;
- table usage 18/18;
- 0 new log errors;
- streamed text rebuilt exactly at every snapshot.

The two SQLite S6 failures were a harness race, not a Stop failure. The script
waited up to 120s for a "Creating Data" label that never registered as
visible, and by then the 28s turn had already finished. The agent completed
the turn normally (success). With the wait capped at 25s, the re-run passed:
Stop acknowledged in 1346 ms, run `stopped` with `sigkill`.

## Unit tests

`backend/tests/unit/test_slim_context_snapshot.py` (3 tests): the slim
snapshot keeps the usage summaries, never the full schema or instruction
sections, and handles an empty view.
