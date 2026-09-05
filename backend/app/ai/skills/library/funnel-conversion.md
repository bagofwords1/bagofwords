---
key: funnel-conversion
title: Funnel and conversion analysis
description: Use when asked about conversion rates, drop-off or a multi-step journey — define the funnel before measuring it.
category: general
version: "1.0"
tags: [analysis, conversion]
---

A funnel is a set of choices disguised as a chart. Two analysts measuring "the
same" funnel routinely differ by tens of percent, entirely because of decisions
neither wrote down.

## 1. Make the four choices, and state them

1. **The steps**, in order, each as a specific event or state — not a label.
2. **Ordered or unordered?** Must a user hit step 2 *after* step 1 to count?
   (Usually yes; say so.)
3. **The conversion window.** Does a purchase 40 days after the visit count?
   Without a window, the funnel silently favors older cohorts, which have had
   more time — and your "improvement" is just age.
4. **The unit** — user, session, or account. A session funnel and a user funnel
   answer different questions and give very different numbers.

## 2. Compute it two ways and show both

- **Step-to-step rate** (of those who reached step N, what share reached N+1) —
  this is what tells you where the problem is.
- **Overall rate from step 1** — this is what leadership tracks.

Reporting only the second hides where the loss happens; reporting only the
first hides how much it matters.

## 3. Guard against the usual corruptions

- **Entry-cohort the funnel.** Count users by when they *entered*, and give
  every entrant the same window to convert. Mixing users who entered yesterday
  with users who entered last quarter understates conversion.
- **Deduplicate.** One user who retries five times is one user, not five —
  unless you deliberately chose sessions as the unit.
- **Watch for out-of-order and skipped steps.** If a meaningful share of users
  reach step 3 without step 2, the funnel model is wrong; report that rather
  than filtering them away silently.
- **Instrumentation gaps beat behavior.** A step whose event stopped firing
  looks exactly like a step where everyone quits. Check event volume per step
  against the prior period before diagnosing a drop-off.

## 4. Segment the drop-off, then report

The overall funnel is the setup; the value is in which segment drops off
differently — device, channel, plan, geography, new vs. returning. Segment the
worst step and report the contrast.

Deliver: the funnel with both rate types and absolute counts at every step (a
percentage without a count is unactionable), the definitions from step 1, and
the one step where the largest recoverable loss sits. Where a difference is
small and the counts are small, say it is within noise rather than ranking it.
