---
key: ab-test-readout
title: A/B test and experiment readout
description: Use when asked whether a test won — check validity before significance, and never call a result from a peek.
category: general
version: "1.0"
tags: [experimentation, statistics]
---

# A/B test and experiment readout

Most experiment mistakes are not statistical, they are procedural: calling a
test early, testing a metric the experiment cannot move, or reading a broken
randomization as a win. Check validity first; significance is the last step,
not the first.

## What is available

scikit-learn and scipy are available (subject to the org's machine-learning
setting) — scipy covers the tests you need here. **statsmodels is not
installed**, so do not reach for its power or proportion helpers; compute the
comparison directly and say what you used.

## 1. Validity checks — before any p-value

Run these in order. If one fails, the readout is that failure; do not proceed
to significance and mention the problem as a caveat.

- **Sample ratio mismatch.** Compare the actual split to the intended split.
  A meaningful deviation means randomization or logging is broken, and the
  result cannot be trusted regardless of how good it looks.
- **Pre-period parity.** Where possible, compare the groups on a metric from
  *before* the test. Groups that already differed were not comparable.
- **Contamination.** Users appearing in both arms, or exposed before
  assignment.
- **The unit of randomization is the unit of analysis.** If users were
  randomized, analyze users. Analyzing sessions or events from a user-level
  randomization understates variance and manufactures significance.
- **Duration.** Did it run at least one full business cycle (usually a whole
  week, more if weekly seasonality is strong)? A test run Monday to Thursday
  measures Monday-to-Thursday users.

## 2. Never call it from a peek

If the test has not reached its planned sample size or end date, say so and
report the current state as *provisional*. Repeatedly checking a running test
and stopping at significance is how tests "win" that have no effect at all. If
no sample size was planned, say that too, and report what effect the current
sample can actually detect.

## 3. Report effect and uncertainty, not just a verdict

- Lead with the **absolute** difference and the **relative** lift, with a
  confidence interval on the difference. A lift with no interval is not a
  result.
- Report **practical** significance against whatever threshold matters to the
  business, not only statistical significance. A statistically significant
  0.2% lift may not be worth shipping.
- **Not significant means inconclusive, not "no effect."** State the interval —
  a wide interval around zero means the test was underpowered, which is a
  different problem from a flat result.
- **Guardrail metrics.** Check the metrics the change could damage (latency,
  refunds, unsubscribes, support contacts), not only the target metric. A win
  on conversion with a guardrail regression is not a win.
- **Segment cautiously.** Every extra segment cut raises the chance of a false
  positive. Report segment findings as hypotheses for a follow-up test, never
  as results, and say how many cuts you looked at.

## 4. Deliver

One line with the verdict and the caveat that qualifies it; the metric table
(control, variant, absolute diff, relative lift, interval, n per arm); the
validity checks and their outcomes; the guardrails; and a recommendation that
names what you would do next — ship, iterate, or extend — and why.
