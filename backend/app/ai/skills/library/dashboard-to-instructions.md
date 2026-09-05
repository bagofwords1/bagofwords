---
key: dashboard-to-instructions
title: Mine an existing dashboard into reusable instructions
description: Use in training mode to turn a dashboard or past report's queries into reusable metric definitions, join rules and filters.
category: general
version: "1.0"
modes: [training]
tags: [curation, onboarding]
---

# Mine a dashboard into reusable instructions

A dashboard that people trust is a written-down record of how this business
actually defines its numbers. Every `WHERE status != 'test'`, every join path,
every date-column choice is a decision someone made and nobody wrote down.
This skill extracts those decisions so future answers reproduce them instead
of re-deriving them differently.

## 1. Get the source material

`list_agent_executions` is the history. Narrow it:

- `tool_name='create_dashboard'` or `'create_widget'` to find dashboard builds;
  `tool_name='create_data'` for the queries themselves.
- `prompt_search='<topic>'` when the user named a subject area.
- `data_source_ids` when the dashboard belongs to one agent.

Read the generated SQL/code from those executions. The SQL is the evidence —
an instruction proposed without a query behind it is a guess.

## 2. Extract only what generalizes

Read each query and pull out the **decisions**, not the data. For every
candidate, ask: "would this still be true next quarter, on different rows?"

Extract these:

- **Metric definitions** — "revenue excludes refunds and test orders"; the
  exact filter list and the reason.
- **Join paths** — which key joins which table, and any required filter on the
  join (`AND d.is_current = true` on an SCD2 dimension is the classic one that
  silently doubles numbers when forgotten).
- **The date column that counts** — `ordered_at` vs `shipped_at` vs
  `created_at`. Dashboards make this choice; nobody documents it.
- **Enum meanings** — what `status = 'C'` means, which values are terminal.
- **Standard exclusions** — internal accounts, test tenants, cancelled rows,
  a specific region that is reported separately.
- **Grain** — "one row per order line, so revenue must be summed before
  counting orders."

Never extract:

- Record-level facts ("customer 4471 is a test account" — that belongs in the
  data, and the tool rejects it as overfit). Write the general rule instead:
  "accounts flagged `is_internal` are excluded from customer counts."
- Current values ("revenue was $2.1M in July"). It will be wrong next month.
- Anything that appears once and looks incidental. One query is a data point;
  a rule needs to be a rule.

## 3. Check coverage before writing

Duplicated and contradicting instructions are worse than missing ones. For each
candidate, `search_instructions` first. Then:

- Already covered, same meaning → skip it. Say you skipped it and why.
- Covered but **contradicting** → do not add a second rule. `edit_instruction`
  to resolve, and state the conflict explicitly in your summary so a human can
  overrule you.
- Partially covered → extend the existing instruction rather than creating a
  neighbour.

## 4. Write them

`create_instruction`, one rule per instruction. A good one is a general rule
with its scope attached:

- **Title**: the term being defined ("Net revenue", "Active customer").
- **Text**: the rule, then the exact filter/join expression, then one line of
  why. Markdown is fine.
- **Category**: `general` for business definitions and terminology, `code_gen`
  for join patterns, dialect quirks and cast/NULL handling.
- **References**: attach the tables the rule applies to — that is what makes it
  retrievable later for a question about those tables.
- **Evidence**: cite the execution or query you took it from. A reviewer must
  be able to check your work without re-reading the whole dashboard.
- **Confidence**: below 0.7, do not write it — ask instead.

## 5. Report what you did

List: instructions created, instructions skipped as already covered, conflicts
found (these need a human), and decisions you saw but could not generalize
safely. The last list is the useful one — it is where the dashboard is doing
something nobody can justify, and it is worth a human's attention.

Finally, consider whether the same dashboard should also become eval cases (the
"Dashboard to eval cases" skill) — the definitions you just wrote down are only
enforced if something tests them.
