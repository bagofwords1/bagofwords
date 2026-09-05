---
key: usage-review
title: Review recent agent usage
description: Use in training mode to review the last N days of agent activity — what was asked, what failed, and what to fix first.
category: general
version: "1.0"
modes: [training]
tags: [quality, operations]
---

A usage review answers three questions: what are people actually asking, where
is the agent failing them, and what one change would help most. It is the input
to curation — done well, it hands you a ranked list of instructions to write.

## What the tool gives you, and what it does not

`list_agent_executions` returns, per run: the user's prompt, status, thumbs
up/down with any message, the tools called, the step titles, tool success and
failure counts, the report, the user, and the timestamp. Filter it with
`start_date` / `end_date`, `tool_name`, `prompt_search` and `data_source_ids`,
and page through with `page` / `page_size` (50 max).

It does **not** return tokens or cost. There is no agent-facing tool for spend —
LLM usage and cost live in the product's Monitoring → Cost console, scoped per
model and per agent. So when someone asks "what did this cost", say plainly that
it is not available here and point them there; never estimate spend from run
counts, and never imply the totals below are financial.

## 1. Fix the window and the scope

Default to the last 7 days unless asked otherwise, and state the exact dates you
used. Narrow to `data_source_ids` when the question is about one agent. Page
until you have the whole window — a first page sorted by recency is not a
review, and reporting rates from a partial sample is how a review misleads.

## 2. Count before you interpret

Establish the base numbers first: total runs, distinct users, runs per agent,
and the failure rate (`status` plus `total_failed_tools > 0`). Then the same
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
Close with what you could not determine from the history, and say once, plainly,
that cost is not in this data.

Where the fix is clearly right and evidenced by several runs, write it
(`create_instruction`, checking `search_instructions` for existing coverage
first) and say which ones you wrote. Where it is a judgment call, propose it and
leave it.
