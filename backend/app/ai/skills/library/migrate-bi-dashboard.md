---
key: migrate-bi-dashboard
title: Migrate a BI dashboard to the agent
description: Use in training mode to turn a Tableau, Power BI, Qlik or BusinessObjects dashboard into agent knowledge — definitions, then eval cases.
category: general
version: "1.0"
modes: [training]
tags: [migration, onboarding]
---

A dashboard people trust is a written-down record of how this business defines
its numbers. Every `WHERE status != 'test'`, every join path, every date-column
choice is a decision someone made and nobody documented. Migrating it is not
rebuilding the pictures — it is moving that knowledge into the agent so future
answers reproduce it instead of re-deriving it differently.

## 1. Read the source, do not eyeball it

Where the BI system is connected, query its metadata rather than working from a
screenshot. What you need before writing anything:

- **Every measure's formula**, followed down through the calculated fields it
  depends on. The interesting logic is usually two levels below the name.
- **Filters at every level** — report, page, visual, and default parameter
  values. A filter set at report level is invisible in the visual and is the
  most common reason a rebuilt number differs.
- **The date column and any relative-date logic** ("last 12 complete months" is
  not "last 365 days").
- **Join paths and relationship directions**, including many-to-many that the
  source tool resolves with its own semantics.
- **Row-level security.** A report showing different numbers per viewer cannot
  become one definition — surface it rather than flattening it.

Ask for a screenshot or export of the current numbers. Without a target to hit
you cannot tell a successful migration from a confident one.

## 2. Extract what generalizes

For each candidate, ask: would this still be true next quarter, on different
rows? Keep the metric definitions, join rules, date-column choices, enum
meanings, grain and standard exclusions. Drop current values, one-off filters,
and anything that appears once and looks incidental — one query is a data point,
a rule is a rule.

`search_instructions` for each before writing. Already covered and consistent →
skip it, and say you did. Covered but **contradicting** → do not stack a second
rule; resolve it with `edit_instruction` and call the conflict out for a human.
Partially covered → extend the existing instruction rather than growing a
neighbour.

Then `create_instruction` per rule, with the tables attached and the source
report cited as evidence.

## 3. Reproduce the numbers, including the parts you disagree with

Reproduce the legacy logic even where the legacy logic is wrong. If a measure
double-counts, reproduce the double count and raise it separately — changing a
definition during a migration means every number differs and nobody can tell
which differences are bugs.

Then reconcile: for every measure, and a handful of representative slices
(a few dimension values, several periods), compare legacy value against what the
agent now produces. All zero → migration verified, and say it that way with the
table as proof. Any non-zero → investigate before shipping, in order of
likelihood: a missed report-level filter, a different date column or relative
date boundary, a join fan-out, null-handling (the source tool may treat NULL as
zero in aggregates), rounding, or a unit/currency conversion.

Never call a migration complete with unexplained differences outstanding. List
them with your best diagnosis and let the owner decide.

## 4. Lock it in with evals

Reconciled numbers are ground truth that will not survive on its own. Turn the
handful that matter into eval cases (see the "Create eval cases" skill) so the
next schema change tells you it broke them.

## 5. Report

Instructions created, instructions skipped as already covered, conflicts found
(these need a human), reconciliation table, and the differences you could not
explain. Only after that, propose improvements to the dashboard's design — as
separate work, never mixed into the migration.
