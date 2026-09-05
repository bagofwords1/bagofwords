---
key: dashboard-to-evals
title: Turn a dashboard into eval cases
description: Use in training mode to convert a trusted dashboard's questions and numbers into eval cases that catch regressions.
category: general
version: "1.0"
modes: [training]
tags: [evals, quality]
---

Nobody writes evals from a blank page, which is why most workspaces have none.
A dashboard people already trust is the cheapest source of ground truth
available: someone validated those numbers once, and the questions behind them
are exactly the ones users will ask again.

## 1. Recover the questions and the answers

`list_agent_executions` with `tool_name='create_dashboard'` / `'create_widget'`
/ `'create_data'` gives you the prompts users actually typed and the SQL that
answered them. For each widget worth testing you need three things:

1. The **question**, phrased as a user would type it.
2. The **query** that produced the current number.
3. The **current value** — re-run the query with `create_data` rather than
   trusting a number that was captured months ago. An eval pinned to a stale
   value fails on day one and gets ignored.

## 2. Choose what is worth testing

Do not convert every widget. Pick, in this order:

- Metrics with a **contested definition** (the ones the instructions you wrote
  are about). These are what regress.
- Metrics feeding a **decision** — anything in a board pack or a target.
- Questions that have **been asked repeatedly** in the execution history.

Skip: vanity counts, anything whose value legitimately changes hour to hour
without a stable relationship, and widgets built from a one-off upload.

## 3. Write the case

`create_eval` takes the question and the expectations separately. The most
common mistake is putting the meta-instruction in the prompt: the prompt is
what the **user** asks ("What was net revenue in Q2 2026?"), never "create an
eval that checks revenue".

Choose the matcher for what actually matters:

- **A number that should be stable** → a `number.cmp` rule, and prefer a
  bounded range (two rules, `gte` and `lte`) over exact equality unless the
  value is genuinely fixed. An exact match on a live metric is a test that
  fails for the wrong reason every week.
- **The method, not the value** → a `tool.calls` rule (it must call
  `create_data`) plus a `field` rule asserting the generated code contains the
  required filter or join. This is how you pin "revenue always excludes
  refunds" — it survives the number changing.
- **A judgment that resists a matcher** → a `judge` rule with a specific
  assertion ("the answer states the date range it used"). Keep judge prompts
  narrow; a vague judge prompt produces a flaky verdict.
- **An instruction must have been consulted** → a `tool.calls` rule on
  `read_instruction`.

Prefer several small rules over one clever one. When a case fails you want it
to say *which* property broke.

## 4. Prove the case before you keep it

Run it with `run_eval` and read the result with `get_eval_run`.

- **Passes** → keep it. You now know the case reflects current behavior.
- **Fails** → you have learned something *now*, before it mattered. Either the
  expectation is wrong (fix it) or the agent genuinely disagrees with the
  dashboard (that is a finding — report it, do not paper over it by loosening
  the rule until it passes).

A case that was never run is not an eval; it is a note. Never leave one behind
unrun.

## 5. Report

Per case: the question, what it asserts, and its first-run verdict. Then
separately: any dashboard number the agent could not reproduce, with both
values and your reading of why they differ. That list is the real output of
this exercise — it is a list of things the org believes that its data does not
currently support.
