# Run-activity dot on /reports/:id: stable for the whole live run

## Problem
The black circle that says "the agent is working" went missing for 31–64% of
a live run, changed size from 12px to 10px partway through, and moved from
above the blocks to below them. The cause was two separate dots with
different rules:

- `RunActivityToggle` (12px, static, *above* the blocks) only while there was
  no tool step **and** no block had text yet.
- A tail dot (10px, pulsing) only once a tool step existed, **and** hidden
  whenever the newest block held text or a visible tool was running.

So any silence after the run had said something (the next planner round,
code generation, the wait before the answer) showed nothing at all.

## Fix
One dot (`data-testid="run-working-dot"`, 12px, pulsing, reduced-motion
safe) at the tail of the assistant message. It is on for every frame where
the completion is `in_progress`, and goes away only when the run ends, is
being stopped (`sigkill`), or is waiting on the user (a confirmation card).
Rule: `frontend/composables/runWorkingDot.ts`; unit test:
`frontend/tests/unit/runWorkingDot.mjs`.

## Loop
```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py --demo
STUB_DS_ID=<music store id> uv run python ../tools/agent/stream_indicator_stub_llm.py &
uv run python ../tools/agent/setup_stub_llm.py --stub-url http://127.0.0.1:9099/v1
cd ../frontend && VIDEO=1 node ../tools/agent/stream_indicator_probe.mjs <out> after
```
The stub streams seven shapes, picked by `[scenario:<name>]` in the prompt:
`direct`, `reasoning`, `text_tool`, `multi_tool`, `parallel`, `reason_tool`,
`long_gap`. The probe samples the newest assistant message every animation
frame while the stop button is shown.

## Result (same stub, same scenarios)
| scenario | missing before | missing after | longest gap before | sizes before → after |
|---|---|---|---|---|
| direct | 32.2% | 0% | 2.2s | 12 → 12 |
| reasoning | 36.9% | 0% | 2.1s | 12 → 12 |
| text_tool | 46.7% | 0% | 2.2s | 12/10 → 12 |
| multi_tool | 62.6% | 0% | 5.9s | 12/10 → 12 |
| parallel | 64.3% | 0% | 6.5s | 12/10 → 12 |
| reason_tool | 38.6% | 0% | 4.3s | 12/10 → 12 |
| long_gap | 32.0% | 0% | 3.7s | 12/10 → 12 |

Before, up to 42.9% of live frames had *no dot and nothing changing on
screen* (`idle_missing_pct`): the run looked dead. After: 0 in every
scenario, the dot never leaves the tail, never duplicates, and never
toggles. Raw numbers and GIFs: `media/pr/claude-busy-darwin-37r4uv/`.

## Not changed
The shared-conversation view (`pages/c/[token]`) still uses
`RunActivityToggle`'s pre-step thinking dot and has no tail dot.
