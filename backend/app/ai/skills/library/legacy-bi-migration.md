---
key: legacy-bi-migration
title: Rebuild a legacy BI report
description: Use when asked to recreate a Tableau, Power BI, Qlik, BusinessObjects or Oracle BI report — reproduce it, then prove the numbers match.
category: dashboard
version: "1.0"
tags: [migration, dashboard]
---

A migration is judged on one thing: does the new number equal the old number?
Rebuilding something that looks similar but computes differently destroys trust
in the whole platform on day one. Reproduce first, improve later — and never
in the same step.

## 1. Read the source report, do not eyeball it

Where the source system is connected, inspect its metadata rather than working
from a screenshot. What you need before building anything:

- **Every measure's formula**, including the calculated fields it depends on.
  Follow the chain to the bottom; the interesting logic is usually two levels
  down in a calculated field nobody mentions.
- **Filters at every level** — report, page, visual, and any default parameter
  value. A filter set at the report level is invisible in the visual and is the
  most common cause of a mismatch.
- **The date column and any relative-date logic** ("last 12 complete months"
  is not "last 365 days").
- **The joins and the model's relationship directions**, including any
  many-to-many that the source tool resolves with its own semantics.
- **Row-level security**, if any. A report that shows different numbers per
  viewer cannot be reproduced as a single number — surface this explicitly.

Ask for a screenshot or an export of the current numbers to reconcile against.
Without a target to hit, you cannot claim a successful migration.

## 2. Map to the warehouse

The source report may query an extract, a cube, or a semantic model rather than
the tables you have. Establish for each field where it comes from, and flag any
field whose source you cannot find — do not substitute a similarly named column
and hope. If the org has a semantic layer, prefer its definitions and note where
they differ from the legacy report's; that difference is a decision for the
owner, not for you.

## 3. Reproduce exactly, including the parts you disagree with

Build it with the legacy logic even where the legacy logic is wrong. If a
measure double-counts, reproduce the double count, and raise it separately.
Changing the definition during a migration means every number differs and
nobody can tell which differences are bugs.

## 4. Reconcile before declaring done

This step is the deliverable. Produce a comparison table: for every measure,
and for a handful of representative slices (a few dimension values, several
periods), show legacy value, new value, and the difference.

- **All zero** → migration verified. Say it that way, with the table as proof.
- **Any non-zero** → investigate before shipping. In order of likelihood: a
  missed report-level filter, a different date column or relative-date
  boundary, a join fan-out, a null-handling difference (the source tool may
  treat NULL as zero in aggregates), rounding/precision, or a currency or unit
  conversion.

Never report a migration as complete with unexplained differences outstanding.
List them, with your best diagnosis for each, and let the owner decide.

## 5. Harvest the knowledge

A legacy report is a written record of business definitions. Once reconciled,
capture the metric definitions, filters and join rules as instructions, and the
key numbers as eval cases — otherwise the knowledge stays locked in a system
you are about to turn off. Then, and only then, propose improvements to the
design as a separate piece of work.
