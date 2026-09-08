---
key: usage-review
title: Review recent agent usage
description: Use in training mode to review the last N days of agent activity — what was asked, what failed, and what to fix first.
category: general
version: "1.0"
order: 50
default_enabled: true
modes: [training]
tags: [quality, operations]
---

A usage review answers three questions: what are people actually asking, where
is the agent failing them, and what one change would help most. It is the input
to curation — done well, it hands you a ranked list of instructions to write.

## What the tool gives you, and what it does not

Use `create_data` targeting `builtin:bow`, table `bow.runs`. It creates a saved
query/table with prompt, status, feedback, judge scores, tokens, cost (nullable,
with a partial-cost flag), tool counts, report/run IDs, user, and timestamps.
Use `bow.tool_calls` for call names, errors, and result previews. Filtering uses
the Diagnosis query grammar: `feedback:negative`, `tools.failed:>0`,
`tool:create_data tool.status:error`, `judge.confidence:<3`, or `agent:"Name"`.
Time is supplied through `time_range` (relative hours/days or explicit bounds).

For totals, groupings, and trends, use the source's server-side `group_by` and
`metrics`. These operate over all matching authorized rows. Never estimate a
population from a top-N table. Row retrieval is bounded and fails explicitly
on overflow; narrow or aggregate, rather than silently omit rows. Null cost is
unknown, not zero, and partial cost must be labeled.

## 1. Fix the window and the scope

Default to the last 7 days unless asked otherwise, and state the exact dates you
used. Narrow with an `agent:"Name"` query predicate when the question is about
one agent. Use server-side aggregates for totals across the whole window;
reporting rates from a top-N sample misleads.

## 2. Count before you interpret

Establish the base numbers first: total runs, distinct users, runs per agent,
and the failure rate (`status` plus `failed_tool_count > 0`). Then the same
figures for the previous window of equal length — a 12% failure rate means
nothing until you know whether last week was 4% or 20%.

## 3. Read the failures, not just the count

Group failing runs by what they have in common — the same tool failing, the same
agent, the same kind of question. Then read the actual prompts. The categories
worth separating, because each has a different fix:

- **Missing knowledge** — the agent had no definition for a term and guessed or
  clarified. → an instruction.
- **Wrong or ambiguous schema mapping** — it picked the wrong table or join.
  → an instruction attached to those tables.
- **Genuine tool or data failure** — a connection down, a query timing out, a
  permission error. → not a curation problem; report it as an operational one.
- **Out of scope** — the agent was asked something its data cannot answer.
  → a coverage question for the owner, not a bug.

## 4. Take negative feedback literally

Runs with `feedback_direction` negative are the highest-signal rows in the whole
window, and there are usually few enough to read every one. Read the message
alongside the prompt and the tools called. A thumbs-down on a run that *looks*
successful is the most valuable thing here: it means the answer was wrong in a
way no status code caught.

## 5. Find the repeats

Cluster the prompts by what they are asking, not by wording. Two outputs matter:

- **Frequent and answered well** → a saved prompt, so nobody retypes it, and an
  eval case, so it keeps working.
- **Frequent and answered badly** → the top of the fix list. Frequency times
  failure rate is the ranking; a rare failure is worth less than a common
  mediocrity.

## 6. Report something actionable

Lead with the numbers and their change against the prior window. Then the
failure categories with counts. Then a ranked list of concrete fixes — this
instruction, on these tables, because of these N runs — not "improve coverage".
Close with what you could not determine from the history, and label missing or partial cost when reporting spend.

Where the fix is clearly right and evidenced by several runs, write it
(`create_instruction`, checking `search_instructions` for existing coverage
first) and say which ones you wrote. Where it is a judgment call, propose it and
leave it.
