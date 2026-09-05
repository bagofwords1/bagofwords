---
key: variance-bridge
title: Variance and bridge analysis
description: Use for budget vs actual, period-over-period or plan-vs-forecast gaps — decompose the delta into parts that sum to it.
category: general
version: "1.0"
tags: [finance, analysis]
---

# Variance and bridge analysis

A bridge answers "what makes up this gap" with components that **add up** to
the gap. That arithmetic constraint is the whole discipline: if the parts do
not sum to the total, the analysis is wrong, no matter how sensible each part
looks.

This is not root cause analysis. RCA asks *why* a number moved; a bridge
decomposes *what* the movement is made of. Do the bridge first — it usually
tells you where to point the RCA.

## 1. Fix the two endpoints

State both explicitly: the baseline (budget, prior period, plan) and the actual,
each with its exact period and scope, and the total variance in absolute and
percent terms. Confirm both endpoints come from sources that agree on scope —
comparing a budget that excludes a subsidiary against an actual that includes
it produces a variance that is entirely definitional.

Establish the sign convention up front and hold it: favorable vs. unfavorable,
not just positive and negative. For a cost line, over-spend is negative even
though the number is bigger.

## 2. Choose the decomposition

- **Price / volume / mix** for revenue and cost lines:
  - *Volume* = (actual qty − base qty) × base price
  - *Price* = (actual price − base price) × actual qty
  - *Mix* = the remainder from the shift in composition between items
  Compute each explicitly and reconcile: the three must sum to the total
  variance. If they do not, you have a fourth effect (new/discontinued items is
  the usual culprit) — add it as its own bar rather than absorbing it into mix.
- **By segment/entity** when the drivers are organizational (region, product
  line, cost center).
- **New / lost / existing** when the population changes between periods.

Always include a **residual/other** bar if anything is left. An unexplained
remainder shown honestly is far better than one silently distributed across the
named components.

## 3. Prove it reconciles

Before presenting, verify: baseline + Σ(components) = actual, exactly. Show
that arithmetic in the output. This is the check a finance reader will do
first, and failing it costs you the whole analysis.

## 4. Deliver

A waterfall from baseline to actual, largest components first, with the
residual last. Alongside it: a table with each component's value and share of
the total variance, and one line per material component saying what drove it.

Do not editorialize on immaterial bars — set a materiality threshold, say what
it is, and group everything below it into "other". Where the biggest component
is itself a puzzle, that is the handoff point to a root-cause analysis.
