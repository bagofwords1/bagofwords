# Re-verification of the instruction-write fixes on `main`

After PR #819 merged, `origin/main` (`ec169f3e`, which also carries #820/#821/#683
on top of the fix) was checked out and the whole thing verified again from a
clean rebuild. All checks pass.

## 1. Fixes present and intact in main's source

- `build_service.py` `promote_build` — the `is_distinct_from` changed-rows filter is present.
- `build_service.py` `add_to_build` — the unique-violation recovery is present.
- `completion_service.py` — the fresh-session streaming error recovery is present.
- No commit touched `build_service.py` / `instruction_service.py` after the fix, so the
  promote path on main is exactly as merged (PR #820's instruction work was in agent_v2 /
  tools / prompts, not the service path).

## 2. Correctness smoke (on main)

- Editing an instruction reflects on the live row. **PASS**
- Editing instruction A leaves instruction B's `updated_at` untouched (the filter works). **PASS**
- A fresh create populates the live row's text + version. **PASS**

## 3. Load behaviour (mock LLM, 2 workers, stock max_connections=100)

Pre-fix baseline (`results/full`) → main (`results/main_verify`):

| | L30 | L60 |
|---|---|---|
| create_instruction p95 | 46.7s → **14.8s** | 32.8s → **16.2s** |
| edit_instruction p95 | 20.6s → **6.8s** | 17.9s → **13.4s** |
| create fail rate | 15.4% → **0.0%** | 34.3% → **16.4%** |
| FOR UPDATE lock wait | ~1,836ms/call → **~434ms/call** | — |
| browse throughput | 190 → **295** | 666 → **1125** |
| too-many-clients | 0 → 0 | 19 → **5** |
| agent p50 | 70.7s → **50.5s** | 78.8s → **78.0s** |
| server-side success | 18/18 | 34/36 → 34/36 |

Run-to-run magnitude varies (small-sample p95, and the browse cohort's achieved
throughput differs), but the direction is a consistent, substantial improvement at
both levels, and correctness is preserved.

## 4. Fix 4 (streaming recovery) — verified via load data

After the main runs: 93 completions `success`, 2 `error`, and **0 stuck `in_progress`**
older than 3 minutes. Pre-fix, those 2 failures would have hung in `in_progress`
forever; on main they are correctly marked `error` on a fresh session.

## 5. e2e tests (on main, sqlite, `-m e2e`)

- `test_build.py` + `test_instruction.py` (cover the promote / add_to_build paths):
  **67 passed, 1 skipped.**
- `test_completion.py::{test_completion_streaming, test_completion_background}`:
  cannot run in this sandbox — they `pytest.fail("OPENAI_API_KEY_TEST is not set")`
  at the top, before touching completion logic. Environment gate (a CI secret), not a
  code failure. Fix 4 is verified functionally via §4 instead.

## Conclusion

All three merged fixes are present, correct, and produce the expected improvement on
`main`. The only unrun tests are gated on a real OpenAI key absent from this sandbox,
and the code path they would exercise is independently verified from the load data.
