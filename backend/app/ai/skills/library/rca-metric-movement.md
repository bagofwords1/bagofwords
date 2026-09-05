---
key: rca-metric-movement
title: Root cause analysis for a metric movement
description: Use when someone asks why a metric moved, dropped, spiked or missed target — decompose the change before explaining it.
category: general
version: "1.0"
tags: [analysis, diagnostics]
---

# Root cause analysis for a metric movement

Explaining a number that moved is not the same as reporting the number. A
plausible-sounding story built on one query is the failure mode here: the
business acts on it, and it is wrong. Work the steps in order and stop at the
first one that explains the movement.

## 0. Pin the movement before explaining it

Never start from the user's framing of the size of the change — confirm it.
Establish and state, with a query:

- **The metric definition.** Scan `<available_instructions>` / `<available_skills>`
  and `read_instruction` anything defining this metric BEFORE writing SQL. If
  the term is undefined and the schema mapping is ambiguous, `clarify` — do not
  invent a definition.
- **Both windows.** The "after" period and the comparison period, as explicit
  dates. "Last month vs the month before" is ambiguous mid-month — say
  `2026-08-01..2026-08-31 vs 2026-07-01..2026-07-31` and use it consistently.
- **The actual delta**: absolute and percent. If the movement is inside normal
  variation (compare against the same delta for the prior 6-12 periods), say so
  and stop. Explaining noise as if it were signal is the most expensive
  mistake in this workflow.

## 1. Rule out data artifacts FIRST

Most "the metric dropped" tickets are pipeline problems, not business events.
Before any business explanation, check and report:

- **Freshness / partial period.** `MAX(created_at)` (or the table's load column)
  per table involved. A month-to-date number compared against a full month is
  the single most common false alarm.
- **Row count of the underlying table** in both windows. A 30% drop in source
  rows is a loading problem, not a demand problem.
- **A new NULL or default.** Null rate on every column in the metric's
  filters and joins, both windows. A column that started arriving NULL silently
  moves every filtered aggregate.
- **Definitional change.** A new category value, a renamed status, a new
  source system that started or stopped loading. `SELECT DISTINCT` on the
  dimension columns for both windows and diff the value sets.

If any of these explain the movement, that IS the root cause. Report it as a
data issue, name the table and column, and stop. Do not continue into business
hypotheses.

## 2. Decompose additively before hypothesizing

Do not guess at causes. Split the total delta into parts that sum back to it,
so every hypothesis is a number and not an opinion.

Split by each available dimension in turn — region, segment, channel, product,
customer tier, plan — with a query per dimension returning: `before`, `after`,
`delta`, and `share of total delta`. Sort by absolute contribution.

The output you are looking for is concentration: "the -12% total is -14% from
one region, everything else flat" is a finding. "Everything is down 12%" is a
different finding (something systemic — pricing, a platform change, seasonality)
and rules OUT the per-segment hypotheses.

For a metric that is a product or ratio (revenue = price × volume, ARPU =
revenue / users), decompose into its factors as well, so you can say which
factor moved. Mix effects matter: a total average can fall while every segment
rises, purely because the segment weights shifted. Check for that explicitly
before attributing a change to any segment's performance.

## 3. Check new/lost/existing composition

For a metric summed across entities (customers, accounts, stores, SKUs), split
the delta three ways: entities present in both windows (expansion/contraction),
entities present only before (churn), entities present only after (new). These
three sum to the total delta and usually point straight at the cause.

## 4. Correlate with events — carefully

Only after the decomposition points somewhere: look for what changed at that
time and place. Deploys, price changes, campaign starts/stops, an outage, a
holiday, a competitor event, a policy change. If the org has an operational or
monitoring connection attached, query it for the same window.

Correlation is a hypothesis, not a conclusion. Say which it is. A change that
predates the movement, or that hits a segment that did not move, is ruled out —
state the ruled-out ones too; they are half the value of an RCA.

## 5. Report

Use `create_doc` when the answer needs to be shared or has more than two
findings; otherwise answer in the message. Either way, this order:

1. **What moved** — metric, both windows, absolute + percent delta.
2. **Root cause** — one sentence, leading with the decomposition number that
   supports it.
3. **Evidence** — the contribution table. Every number carries the query that
   produced it (`{{viz:<uuid>}}` embeds in a doc; `create_data` first, then the
   doc).
4. **Ruled out** — hypotheses checked and rejected, with the number that
   rejected each.
5. **Confidence and what would confirm it** — name the query or data you do
   not have. Never round a hypothesis up to a conclusion.
6. **Recommended action**, only when the evidence supports one.

If the decomposition does not concentrate anywhere and no data artifact
explains it, the honest answer is "the movement is broad-based, here is the
distribution, here is what I could not rule out." Say that rather than
promoting the largest-looking segment to a cause.
