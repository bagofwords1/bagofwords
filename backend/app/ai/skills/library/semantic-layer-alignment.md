---
key: semantic-layer-alignment
title: Align answers with the dbt / LookML semantic layer
description: Use when the org has dbt or LookML metadata — prefer its governed metrics and joins over re-deriving them in SQL.
category: data_modeling
version: "1.0"
tags: [dbt, lookml, governance]
---

# Align answers with the semantic layer

When an org has invested in dbt models or LookML views, those definitions are
the governed truth. An answer that re-derives a metric from raw tables and gets
a slightly different number is worse than useless: it makes two internal
sources disagree, and someone spends a day reconciling them.

## 1. Look before you write SQL

If dbt or LookML metadata is synced for this connection, read it first for any
metric, dimension or join you are about to write. `read_resources` /
`read_instruction` on the relevant models. What you are looking for:

- Is there a **model or view that already computes this metric**? Query that,
  not the raw tables underneath it.
- What **filters are baked into** that model (test accounts, soft deletes,
  status exclusions)? Applying them again double-filters; omitting them when
  you query raw tables under-filters.
- What are the **declared joins and their grain**? A many-to-many join that the
  semantic layer handles with a fan-out guard will silently multiply your
  numbers if you write it by hand.
- Is the model **marked deprecated or stale**? Prefer its replacement.

## 2. Prefer the governed model, and say that you did

Order of preference:

1. A metric defined in the semantic layer → use it, and name it in the answer
   ("using the `net_revenue` metric from dbt").
2. A model that exposes the right grain → aggregate from it.
3. Raw tables → only when neither exists. Say that you went to raw tables and
   why, because that is exactly when a number is likely to diverge from the
   BI tool.

## 3. When your number disagrees — flag, do not choose

If you compute a metric that also exists in the semantic layer and the numbers
differ, that is a finding to surface, not a discrepancy to quietly resolve by
picking one. Report both values, the difference, and the most likely cause:
different filters, different date column, different grain, a fan-out join, or
a stale materialization.

Never present your own re-derivation as the answer while a governed definition
exists and disagrees. The org's reporting depends on the governed number; your
job is to make the disagreement visible.

## 4. Feed back what is missing

When a metric the user asks for has no definition anywhere, that gap is worth
recording — propose an instruction capturing the definition you settled on
(with the user's confirmation), so the next answer is consistent with this one
even if the semantic layer never catches up.
