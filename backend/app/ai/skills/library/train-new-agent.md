---
key: train-new-agent
title: Train a new agent
description: Use in training mode to stand up a new agent — pick its scope, then teach it the definitions and checks that make it trustworthy.
category: general
version: "1.0"
modes: [training]
tags: [onboarding, agents]
---

Creating an agent takes one tool call. Making it *useful* is the rest of this
skill: a new agent knows the schema and nothing about how the business reads it,
so its first answers are plausible and subtly wrong until someone teaches it.

## 1. Scope the agent before creating it

`list_connections` for what exists, then `get_connection` for the catalog of the
one you're building on. Then settle three things — ask through `clarify` when
they aren't stated, with the schema/prefix groups AND their table counts as
clickable options:

- **Name**, in the users' vocabulary ("Revenue", not "dwh_prod_fct").
- **Coverage.** Which schemas, tables or (for MCP/API connections) tools.
  Narrow beats broad: an agent pointed at 900 tables answers slowly and picks
  the wrong ones. Start with the subject area people actually ask about.
- **Audience.** `is_public` for the whole org, private otherwise.

Then `create_agent` once, with `schemas` / `tables` / `tools` set — not a
default-everything agent you prune later.

## 2. Teach it what the schema cannot say

This is where the agent becomes trustworthy. Walk the tables you activated and
capture, as instructions:

- **The grain of every fact table**, in one line ("one row per order line").
  Getting this wrong is the single most common cause of doubled revenue.
- **The metric definitions people argue about** — what counts as revenue,
  active, churned, complete. Include the exact filters.
- **The date column that counts** for each fact — created vs. shipped vs.
  recognized. Dashboards make this choice silently; write it down.
- **Join paths**, including any filter the join needs (`AND d.is_current` on an
  SCD2 dimension is the classic one that silently multiplies rows).
- **Enum meanings** — what `status='C'` is, which values are terminal.
- **Standard exclusions** — test accounts, internal orgs, cancelled rows.

Rules only. Never a record-level fact ("customer 4471 is a test account") — the
tool rejects those as overfit, and rightly: they belong in the data. Write the
general rule instead ("accounts flagged `is_internal` are excluded from customer
counts"). Attach each instruction to the tables it governs, so it is retrievable
for questions about them.

`search_instructions` before writing each one — an org with other agents likely
has the definition already, and a second copy that drifts is worse than none.

## 3. Give it a way to be wrong out loud

An agent with no evals regresses silently. Before you hand it over, add a few
cases with `create_eval` (see the "Create eval cases" skill for the rubric) that
cover the definitions you just wrote, and `run_eval` them. A definition nothing
tests is a definition that will quietly stop holding.

Saved prompts (`create_prompt`) are worth adding for the questions this audience
asks weekly — they double as documentation of what the agent is for.

## 4. Hand it over honestly

Say what the agent covers, what it deliberately does not, which definitions you
recorded (and which you guessed at and want confirmed), and what the evals check.
The guessed definitions are the important list — they are where the agent will be
wrong first, and the owner is the only person who can settle them.
