# Before/after eval — coder-based (origin/main 75aabf4) vs planner-authored (branch head)

Same harness for both conditions: Claude 4.5 Haiku only, Chinook SQLite data
source, identical turn scripts driven through the real chat UI (Playwright),
fresh report per run, fresh DB for the OLD condition, scored by
`../scorer.py` (architecture-neutral). Raw score JSONs sit alongside this file.

| Scenario | OLD (main) | NEW (branch) |
|---|---|---|
| design_oneshot (complex one-shot build) | 0.933 | **1.000** |
| memory_gauntlet (12-turn long memory) | 0.571 | **0.857** |
| destructive_gauntlet (8-turn add/remove/restyle) | 0.900 | **1.000** |
| **mean** | **0.801** | **0.952** |

## What OLD failed on — exactly the reported complaint classes

memory_gauntlet (0.571):
- **2 of 4 payload visualizations unreferenced in the head code** — charts
  silently dropped from the page across iterations.
- **"PERMANENT RULE: indigo accents" lost** — pinned in turn 3, gone by turn
  12; the final assistant message *claimed* "✓ Indigo (#6366f1) accents"
  while the rendered dashboard is default blue-on-white (the two-minds
  failure: the coder asserts what it cannot check).
- **Zero server-side parameters declared** — the requested "re-run the data
  server-side" genre/country filters shipped as client-side controls; the
  genre filter then disappeared entirely by the final version (replaced by an
  unrequested date-range filter).
- Rendered KPIs showed **INVOICE COUNT 0 / DISTINCT CUSTOMERS 0** — wrong
  data rendering confidently (screenshot in the run log).

destructive_gauntlet (0.900): the turn-4 emerald KPI accent was lost by later
edits (no emerald token, word or hex, in the head code).

design_oneshot (0.933): built well and declared both server-side params, but
the live filter-interaction check could not find a working selectable filter.

## What NEW was dinged on — harness artifacts, not regressions

memory_gauntlet (0.857): `min_visualizations` (the planner used the
one-wide-master-table pattern its prompt encourages — a scenario-check
misfit, all sections derive client-side and render), and the interactive
check could not find a literal "Rock" option to select (both server-side
params ARE declared and the controls render). Every retention check passed:
Momentum, indigo, rounded-xl survived to v11; the removed chart stayed
removed; KPI row stayed first.

## Effort (same ballpark — robustness did not cost speed)

| | OLD wall s (3 runs) | NEW wall s | OLD tool errors | NEW tool errors |
|---|---|---|---|---|
| totals | 96+372+311 | 115+412+308 | 0+0+1 | 0+4+6 |

NEW's higher tool-error count is the gates working: rejected ops/renders that
the planner corrected in-loop — every scenario turn still landed a version,
and nothing invalid persisted. OLD's low error count reflects the absence of
gates: its failures shipped silently instead of erroring.

## Caveats

- n=1 per cell, one model (Haiku), one dataset. Directional, not statistical.
- The interactive filter check is conservative (native `<select>` with an
  exact "Rock" option); "skip" is scored as a miss for both conditions.
- OLD ran on a fresh DB with identical seeding; prompts were each
  architecture's own.
