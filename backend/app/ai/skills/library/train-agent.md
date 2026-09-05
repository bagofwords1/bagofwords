---
key: train-agent
title: Train an agent
description: Use in training mode to teach an existing agent — read its tables, inspect real values, ask what the data cannot say, then write the instructions.
category: general
version: "1.0"
modes: [training]
tags: [onboarding, instructions]
---

The agent and its tables are already set up. What is missing is everything the
schema cannot say: which column actually means revenue, what `status = 'C'` is,
which date counts, which rows nobody wants counted. Until that is written down
the agent's answers are plausible and subtly wrong, and nobody can tell which.

The work is: read the tables, look at the real values, ask about what is left
ambiguous, and write it down as instructions. Do not skip to writing — an
instruction guessed from a column name is worse than no instruction, because it
looks authoritative.

## Keep a note open from the first step

`create_note` at the start, `edit_note` as you go. Put in it: a `- [ ]` list of
the tables to cover, findings per table (grain, keys, enum meanings, oddities),
open questions for the user, and answers as they come back. You will look at
more tables than you can hold, and the note is what stops the last table you
read from dominating what you write.

It is also what makes the session reviewable: someone reading the note should
be able to see why each instruction exists.

## 1. Read every table

`describe_tables` across the agent's tables — not a sample. For each one record
in the note: what it appears to hold, its columns and types, the apparent key,
and which other table it looks joinable to.

Two things matter more than the column list:

- **The grain.** One row per what? Get this wrong and every aggregate built on
  it is wrong. If the column names do not settle it, the next step will.
- **Fact vs. dimension.** Which tables carry measures and which carry
  attributes — it determines every join rule you are about to write.

## 2. Inspect the actual values

This is the step that separates a real instruction from a guess. `inspect_data`
against the tables, asking for what you actually need to know:

- **Distinct values of every status, type, flag and category column.** A column
  called `status` with values `A/C/X` means nothing until you have seen them —
  and their meanings are a clarification question, not an inference.
- **Row counts, and distinct counts of the candidate key.** Equal → the grain
  is one row per that key. Not equal → the grain is finer than the name
  suggests, and that is a finding worth writing down on its own.
- **Null rates** on anything you would filter or join by. A column that is 40%
  null changes what a filter on it means.
- **Ranges and outliers** on measures and dates: min, max, negatives, zeros,
  far-future dates. Negative amounts usually mean refunds or reversals — which
  is a definition question.
- **Join integrity.** For each candidate join, how many child rows find a
  parent. A large unmatched share means the relationship is not what the naming
  implied.
- **Date columns.** Where a table has several (`created`, `updated`, `shipped`,
  `posted`), see how far apart they run. That difference is exactly why "which
  date counts" has to be asked rather than assumed.

Record every answer in the note as you get it.

## 3. Ask what the data cannot tell you

Now you know what is ambiguous, and the questions are specific rather than
generic. Bundle them into one `clarify` call rather than trickling them out —
give the options you found in the data as the choices, since that is far easier
to answer than an open question:

- Enum meanings: "`status` has values A, C and X — which of these count as a
  completed order?"
- The metric definitions people argue about: what counts as revenue, active,
  churned, complete; whether refunds, internal accounts and test rows are in
  or out.
- Which date column the business means for each fact.
- Which of two plausible join paths is the correct one.
- Anything the inspection made suspicious: the 40%-null column, the negative
  amounts, the orphaned keys.

Do not guess a definition to avoid asking. A wrong definition, written
confidently into an instruction, propagates into every answer afterwards.

## 4. Write it down — one primary instruction first

`search_instructions` before writing anything: an org with other agents may
already define these terms, and a second copy that drifts is worse than none.
Where something is already covered, skip it and say you did; where a new
finding **contradicts** an existing instruction, resolve it with
`edit_instruction` and flag the conflict for a human rather than leaving two
rules standing.

Then **prefer one large primary instruction** for the agent. A single document
covering the domain — what the tables hold, the grain of each fact, the metric
definitions, the join rules, the standard exclusions — is easier for a person
to review and keeps related rules from contradicting each other. Structure it
with markdown headings so it stays navigable as it grows.

Once it exists, ask the owner to mark it as the agent's **primary
instruction** (set from the agent's instruction view — there is no tool for
it). Primary means it is the document the agent leads with.

**Split when complexity demands it, not by default.** Good reasons to split:

- A rule that is **table-specific** and only relevant to questions about that
  table — attach it to that table so it is retrieved with it.
- A **subject area** big enough to stand alone (billing vs. fulfilment), where
  one document would bury both.
- A rule with a **different lifecycle** — something seasonal, or owned by a
  different team, that will be edited on its own schedule.
- A **code-level** rule (see below): those are a different category and belong
  apart from domain definitions.

Rules only, never record-level facts. "Customer 4471 is a test account" is
rejected as overfit and belongs in the data; the rule is "accounts flagged
`is_internal` are excluded from customer counts". Cite the inspection that
supports each one in `evidence`, and do not write anything you are below 0.7
confident in — ask instead.

## 5. Capture the coding errors too

Domain definitions are only half of what makes an agent reliable. The other
half is the SQL that keeps breaking. `list_agent_executions` with
`total_failed_tools` shows where generation failed; read the failures and look
for the ones that will recur:

- Dialect quirks — date functions, string concatenation, `LIMIT` vs `TOP`,
  identifier quoting for names that need it.
- Type handling — a numeric stored as text needing a cast before it sums,
  timezone-aware vs. naive timestamps.
- NULL semantics that bit the query, and the join filter that must always be
  applied (`AND d.is_current` on an SCD2 dimension is the classic one).
- Anything that failed, was fixed, and would fail again the same way.

Write these as separate instructions with category **`code_gen`**, scoped to
the tables they concern. Keep them mechanical — the failing pattern, the
correct pattern, one line of why. They belong apart from the domain document:
different audience, different lifecycle, and mixing them makes both harder to
review.

## 6. Hand it over

Say what you covered and what you did not, list the instructions you wrote and
where each came from, and name the ones resting on an **assumption the user has
not confirmed** — that list is where the agent will be wrong first, and the
owner is the only person who can settle it.
