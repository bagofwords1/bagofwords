---
key: data-freshness-preflight
title: Data freshness and trust pre-flight
description: Use before reporting any number that feeds a decision — check recency, completeness and sanity, and state staleness in the answer.
category: general
version: "1.0"
tags: [data-quality, trust]
---

The most damaging wrong answer is not a bad query — it is a correct query over
data that stopped loading three days ago, reported with total confidence. This
check is cheap, and it is the difference between an answer someone can act on
and one that quietly misleads them.

Run it before reporting a number that feeds a decision, and always before
concluding that a metric moved.

## The four checks

1. **Recency.** `MAX()` of the table's load or event timestamp, per table
   involved. Compare it against the expected cadence. Report the actual
   as-of moment — "as of the latest row, 2026-09-03 14:00" — not the wall
   clock. If a table is behind its cadence, that is the headline, not a
   footnote.

2. **Partial periods.** If the requested window includes today, or the current
   month/quarter, the period is incomplete. Say so, and either compare
   like-for-like (month-to-date vs. the same days last month) or exclude the
   partial period. An incomplete period compared against a complete one is the
   single most common false "the metric dropped".

3. **Volume sanity.** Row count for the current period against the trailing few
   periods. A large unexplained swing in row count means a loading problem
   until proven otherwise — investigate it before interpreting any aggregate
   built on those rows.

4. **Null and default spikes.** Null rate on the columns used in the metric's
   filters, joins and grouping, current period vs. prior. A column that starts
   arriving NULL, or defaulting to a placeholder, silently moves every filtered
   aggregate without changing any row count.

## Reporting

- **Everything healthy** → one short line with the as-of timestamp. Do not turn
  a clean check into a paragraph.
- **A problem found** → lead with it. Name the table, the column, the expected
  vs. actual, and what it does to the number you were asked for. Then either
  give the number with an explicit caveat, or say plainly that the number is
  not currently reliable. Do not bury a data problem underneath the answer.

Where the same staleness will recur, propose a scheduled freshness check rather
than repeating this by hand.
