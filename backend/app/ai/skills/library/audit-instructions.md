---
key: audit-instructions
title: Audit the instruction set
description: Use in training mode to check an agent's instructions against real usage — find gaps, contradictions and overfit rules, and rank what to fix.
category: general
version: "1.0"
modes: [training]
tags: [quality, instructions]
---

An instruction set decays in two directions at once. Rules that were written
get stale, overfit or contradictory; questions people actually ask never get a
rule at all. This audit looks at both: read what is written and judge it, then
read what was asked and find what is missing.

It is not a usage review. That skill asks what happened last week and what
broke; this one asks whether the written knowledge is any good and where the
holes are. If you have already run a usage review for the window, reuse its
clusters rather than recounting them.

## Know what you cannot see, before you claim anything

Three limits shape every conclusion here. State them in the report rather than
working around them:

- **You cannot read the agent's answers.** `list_agent_executions` returns the
  prompt, status, tools called, step titles and feedback — never the completion
  text, and the report-reading tools are chat-only, not available in training
  mode. So "the answer was wrong" is knowable only from a thumbs-down or a
  failed tool, never by reading it yourself.
- **You cannot see which instruction a run used.** No tool exposes
  per-instruction usage. A run's `tool_names` may show `read_instruction` was
  called, but not which id came back. So **never report that an instruction is
  unused** — you have no evidence for it, and deleting on that basis destroys
  working knowledge.
- **You cannot see what the agent asked.** A `clarify` in `tool_names` tells
  you the agent stopped to ask; it does not tell you the question. The user's
  prompt plus the topic is the whole of the evidence.

What survives those limits is still plenty: the instruction text itself, which
you can read in full, and the distribution of prompts, failures and negative
feedback across topics.

## Keep a note open

`create_note` first, `edit_note` as you go. Track: the instructions enumerated
so far, one line of verdict each, the prompt clusters, and the gaps as they
emerge. An audit means holding thirty documents against a hundred prompts at
once, and without the note the last thing you read wins.

## 1. Enumerate the instruction set

`search_instructions` with no `query` lists everything, but `limit` caps at 50
and there is no paging. Check `total` against what you got back: if it exceeds
what was returned, partition the sweep by `category` (`general`, `code_gen`,
`visualization`, `dashboard`, `system`) and by `data_source_ids`, and say in
the report if you still could not cover it all. An audit over an unstated
sample is worse than no audit.

The results carry the full `text`, so most of the reading is already done.
Use `read_instruction` for anything truncated and for the skills in
`<available_skills>` — installed skills are part of the written knowledge and
can conflict with an instruction just as easily.

Note per row: id, title, category, `load_mode`, `status`, the tables it names,
and whether it carries a `pending_edit`. A staged edit means someone is already
fixing it — do not propose the same change again.

## 2. Judge each instruction on its own

Read for the failure modes that make an instruction actively harmful, not just
useless:

- **Overfit to records.** "Exclude customer 4471", "Maria's team is EMEA",
  "revenue was 1.2M in March". These rot the moment the data moves and they
  teach the agent facts instead of rules. The fix is the general rule the
  observation was an instance of.
- **Volatile facts** — row counts, current date ranges, live metric values.
  Same problem, slower.
- **Vagueness.** "Be careful with joins", "use the right date column",
  "prefer accurate results". A rule the agent cannot act on differently from
  its absence is noise that costs context. Either make it specific or say it
  should go.
- **No table attachment on a table-specific rule.** A join rule or a column
  semantic with no `table_names` will only be retrieved by luck. This is the
  most common real defect and the cheapest to fix.
- **`load_mode: always` overused.** Always-loaded rules are in every prompt
  forever. Reserve it for rules that apply to every question; anything
  table-specific or topic-specific belongs on `intelligent`.
- **Duplicates that have drifted.** Two instructions defining the same term
  differently is worse than either alone — the agent will use whichever it
  retrieves. Search by the term, not by the title, to find these.
- **Direct contradictions**, including against a skill body. Flag every one;
  never quietly pick a side.

## 3. Pull the usage window and cluster it

`list_agent_executions` over the last 30 days by default — a shorter window
finds noise, not gaps. Scope with `data_source_ids` when auditing one agent,
page to the end of the window, and state the exact dates.

Cluster the prompts by what is being asked, not by wording. For each cluster
record: how many runs, how many failed (`status`, `total_failed_tools`), how
many carried a negative `feedback_direction`, and how many called `clarify`.

## 4. Match clusters against the instruction set

This is the audit proper. For each cluster, ask which instruction would have
answered it, and go find that instruction. Three outcomes:

- **Covered and working** — a rule exists, the cluster runs clean. Nothing
  owed, and it is worth an eval case so it stays that way.
- **Covered and failing** — a rule exists and the cluster still fails or draws
  thumbs-down. The rule is wrong, too vague to act on, or unattached to the
  tables the question touches. This is the most valuable finding in the audit,
  because the fix is a specific edit to a specific document.
- **Not covered** — nothing in the set speaks to it. A gap, and its weight is
  how often it is asked.

A cluster that is heavy on `clarify` calls is the signature of a missing
definition: the agent knows it does not know. It cannot tell you what it asked,
but the topic plus the frequency is enough to name what needs writing.

## 5. Rank by frequency times damage

Do not hand over a flat list. Order by how often the cluster comes up times how
badly it goes — a common mediocrity outranks a rare failure. Contradictions and
overfit rules jump the queue regardless of volume, because they produce
confident wrong answers rather than visible ones.

## 6. Fix what is clearly right, propose the rest

Where a defect is unambiguous and the correction is evidenced — an unattached
table rule, an overfit record fact, a rule that contradicts one the user has
confirmed — fix it with `edit_instruction`, anchored on a short unique snippet,
with one sentence of `evidence` naming the runs behind it. Where the gap is
real and the definition is not in question, write it with `create_instruction`.

Everything else is a proposal, not an edit. A definition you would have to
guess at, two rules that contradict where you cannot tell which is intended, an
instruction that looks wrong but that a domain owner may have meant — these go
to the user, ideally as one `clarify` with the options you found rather than a
trickle of questions.

## 7. Report it

Lead with the shape of the set: how many instructions, by category, how many
you were able to read. Then the defects found, grouped by type with ids. Then
the ranked gaps with their evidence — this many runs, this many failures, this
many clarifications. Then what you changed and what you are only proposing.

Close with the limits: the window you covered, any part of the set you could
not enumerate, and plainly that this audit cannot tell which instructions were
actually consulted, so nothing here is a recommendation to delete on grounds of
disuse.
