# Feedback Loop — "add token/usage limit per user on a daily level as well, not just monthly"

Usage policies only capped usage per calendar month (LLM tokens, LLM spend,
data queries, data scanned). One user could burn most of a monthly budget in a
single day. The claim validated here: every metric can now be capped **per day,
per month, both, or neither**; a request is blocked as soon as any cap it would
pass is reached; and the day runs midnight to midnight in the **organization's
timezone** (`OrganizationSettings.config["timezone"]`, UTC fallback).

## Design (validated)

- **Columns** — `usage_policies.daily_{token,query,data_bytes}_limit`,
  `daily_spend_limit_usd` (`backend/app/models/usage_policy.py`, migration
  `dailyquota01`). NULL = no daily cap. A daily cap above the monthly cap of the
  same metric is rejected (422), checked on the merged row so a PUT that only
  lowers the monthly cap is caught too
  (`UsagePolicyService._validate_daily_within_monthly`).
- **Counters** — daily usage lives in `usage_counters` under its own metric name
  (`llm_tokens:daily`, …), scope `organization`, window = today in the org tz
  (`current_day_window`). A separate name is required: in a UTC org the day
  window of the 1st starts at the month window's instant, and the counter's
  unique key would merge the two. Daily counters are recorded whether or not a
  cap is set, so a cap added mid-day sees the day's real usage.
- **Daily data caps count every connection together**; connection overrides
  stay monthly-only.
- **Enforcement** — `UsageLimitContext` keeps daily mirrors of its token/spend
  cache and daily entries in its data cache, includes in-run (unflushed) usage,
  and refreshes when the cached day ends (not only on the 30 s TTL), so a
  long-lived run starts the new day at zero. Monthly is checked first, so when
  both are exhausted the user hears about the one that lasts longer.
- **Wording** — `build_limit_exceeded` words every cap error, e.g.
  `Daily LLM token quota exceeded. Resets at 00:00 Asia/Jerusalem (in 3h 13m).`
  Monthly wording is unchanged; the daily phrases were added to
  `_INTERNAL_QUOTA_MARKERS` (`backend/app/ai/llm/errors.py`) so they classify as
  `usage_cap` (no retry, no model fallback). The classifier test now takes its
  cases from the service's own message table so the two can't drift.
- **Summary** — `usage_quota.daily` on whoami: today's used/limit per metric +
  window + timezone.
- **UI** — Settings › Quotas: Day | Month per metric in the list; the edit modal
  is a Per day / Per month grid with the reset times, inline "daily can't be
  higher than monthly" (Save disabled). Account menu: "Tokens · today" rows and
  "Today resets 00:00 {tz} · month resets {date}". Profile › Usage: "Today: x / y".

## Loop A — deterministic (no external services)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/unit/test_usage_day_window.py tests/unit/test_llm_error_classification.py \
  tests/unit/test_usage_metering_buffer.py -q
uv run pytest tests/e2e/test_daily_usage_limits.py tests/e2e/test_usage_limits.py -m e2e --db=sqlite -q
```

`test_daily_usage_limits.py` covers: API round-trip in every combination,
daily>monthly rejection on create and update for all four metrics,
most-restrictive composition + org tz, each metric × {daily, monthly, both}
blocking at the cap (and not one unit before), daily queries summed across
connections, in-run buffered usage counted before flush, monthly reported when
both are exhausted, yesterday's usage not counting, a running context
crossing midnight, the whoami summary in a non-UTC tz, and the unbuffered
`consume_data_*` path. Before the change these tests can't run at all (no
daily fields/counters exist); after: all pass (see "The fix").

## Loop B — live stack, real agent runs

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py --demo
uv run python ../tools/agent/seed_org.py --sqlite-sources 1 --invite member@example.com --db-path db/agent.db
# Model: Claude Haiku with a funded key (preferred) ...
ANTHROPIC_API_KEY=... uv run python ../tools/agent/setup_haiku_llm.py
# ... or the OpenAI-compatible stub, priced so spend caps are exercised:
STUB_SOURCES_FILE=sources.json uv run python ../tools/agent/stub_llm.py &
uv run python ../tools/agent/setup_stub_llm.py --model-id stub-priced --input-cost 1000 --output-cost 1000
uv run python ../tools/agent/daily_limits_live.py --db-path db/agent.db --timezone Asia/Jerusalem
```

`daily_limits_live.py` runs a baseline prompt and checks today's counters grew
in all four metrics (and match the monthly deltas), then for each metric ×
{daily, monthly, both} sets the cap to exactly the current usage, runs another
prompt and checks it was blocked by that cap with the right wording.

Observed (2026-11-23, stub model; the sandbox's Anthropic key had no credit):

```
PASS baseline run records today's usage in all four metrics {"tokens": 1200, "queries": 2, "data_bytes": 462, "spend": 1.2}
PASS daily and monthly counters move together
PASS daily window is today in the org timezone ["2026-11-22T22:00:00", "2026-11-23T22:00:00"] Asia/Jerusalem
PASS daily tokens cap reached → "Organization usage quota reached: Daily LLM token quota exceeded. Resets at 00:00 Asia/Jerusalem (in 3h 13m)."
PASS daily spend cap reached → "... Daily LLM spend quota exceeded. Resets at 00:00 Asia/Jerusalem ..."
PASS daily queries cap reached → create_data: "Execution error: Daily usage quota exceeded (data queries). Resets at 00:00 Asia/Jerusalem (in 3h 13m)."
PASS daily data_bytes cap reached → create_data: "Execution error: Daily usage quota exceeded (data scanned). ..."
PASS monthly tokens / spend / queries / data_bytes caps → monthly wording (unchanged)
PASS both (daily tight, monthly loose) → blocked by the daily cap, all four metrics
15/15 passed
```

Real Claude Haiku (`setup_haiku_llm.py`, model `claude-haiku-4-5-20251001`).
The sandbox's key has no credit, so Haiku can't *answer*; it still proves the
cap sits in front of the provider call — capped, the run never reaches
Anthropic; uncapped, the identical run does:

```
daily_token_limit set to today's usage = 9300
CAPPED   -> error  claude-haiku-4-5-20251001  "Organization usage quota reached: Daily LLM token quota exceeded. Resets at 00:00 Asia/Jerusalem (in 2h 53m)."
UNCAPPED -> error  claude-haiku-4-5-20251001  "anthropic quota or credit balance exhausted: Your credit balance is too low ..."
```

With a funded key, `daily_limits_live.py` runs unchanged on Haiku (recording
included).

UI (Playwright, `tools/agent/daily_limits_ui.cjs`, as the admin and as a member assigned the "Interns" policy):
list shows Day | Month; creating with daily > monthly shows the inline error
and disables Save; a valid daily/monthly mix saves exactly; edit reloads it;
the member's second prompt after the admin lowers the daily token cap shows
"Daily LLM token quota exceeded. Resets at 00:00 Asia/Jerusalem …"; the account
menu shows "Tokens · today"; Profile › Usage shows "Today: x / y".

Evidence: `media/pr/ai-zen-tesla-zmpmes/` (before/after screenshots,
live-loop, Haiku and Playwright output).

## What this proves / regression notes

- Caps are enforced before the provider call, so the block path is
  model-independent; the stub run exercises the real recording path (real
  agent, real tool executions, real counters).
- Existing monthly behavior and wording are unchanged
  (`tests/e2e/test_usage_limits.py` passes untouched).
- `tests/unit/test_usage_metering_buffer.py` used a hand-written fake of the
  effective-limits object; it gained the three new attributes.
- Wider e2e sweep (`-k "whoami or quota or usage or organization"`): the same
  25 failures/errors (dashboards, org isolation / OIDC sign-in) occur with this
  change stashed — pre-existing and unrelated; with it, 23 more tests pass.

## Review round 1 — four cases the first version missed

| # | Case | Root cause | Fix | Regression test |
|---|---|---|---|---|
| 1 | A run still buffering at midnight: its pre-midnight usage counted against the new day's cap, and the flush charged it to the new day | Pending usage was one running total; the daily check and the daily counter write both used "now" | Buffers keep `(time, amount)`; daily checks count only what happened inside the current day; the flush charges each amount to the day it happened (`_increment_daily_by_day`, data events carry `at`). A `_utcnow()` seam lets tests move the clock | `test_usage_buffered_before_midnight_stays_on_that_day` (all four metrics, both days' totals asserted) |
| 2 | Daily spend + monthly tokens both exhausted → "resets at midnight", but tomorrow is still blocked | The spend check raised before the token check ran | `_llm_breach` evaluates every token and spend cap, then prefers any monthly breach | `test_a_monthly_cap_on_another_metric_wins_over_a_daily_reset` (both mixes) |
| 3 | "150 queries/day" next to "100 per connection/month" rejected | Monthly query/data caps apply per connection; daily ones count all connections, so the comparison mixed scopes | Daily ≤ monthly is checked only for tokens and spend (API `_DAILY_MONTHLY_PAIRS`, UI `COMPARABLE_DAILY_MONTHLY`); the form hint says monthly query/data caps are per connection | `test_daily_data_cap_above_the_per_connection_monthly_cap_is_accepted`; `frontend/tests/unit/quotaLimitInputs.mjs` |
| 4 | Renaming a policy re-saved `$0.004` as `$0` and `1,024 B` as `0 MB` | The editor re-parsed every rounded input on save | Untouched inputs are sent back as their exact stored value (`frontend/utils/quotaLimitInputs.ts` `limitValue`) | `frontend/tests/unit/quotaLimitInputs.mjs`; Playwright `tools/agent/daily_limits_review_ui.cjs` (name-only edit keeps 0.004 / 1024 / 1536) |

After the fixes: backend usage suites 147 passed; frontend unit test passes;
live loop 15/15 again; full Playwright UI flow passes; review UI checks 5/5.
