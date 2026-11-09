---
key: learning-loop
title: Learn rules from labelled examples
description: Use in training mode when the user gives labels or expected answers for items (rows or files) and asks to learn the rules — read all, score, learn, repeat.
category: general
version: "1.0"
order: 15
default_enabled: true
modes: [training]
tags: [instructions, training, evaluation]
---

The user has a target — call it Y — and a set of items. Y can be answers
("Q3 revenue is 4.2M", "these invoices are fraud"), the logic for getting them
("revenue is net of refunds, by booking date"), or both. The items can be rows,
files, tickets, cases — ten of them or thousands. The job is to learn the
instructions that make this agent reproduce Y on its own, and to learn them from
**all** of the items, not the first few.

It is one loop, repeated until it stops improving:

> take a batch → produce the agent's answer → decide whether it matches Y →
> learn from the misses → continue with the updated instructions

The step people skip is the middle one. Finding patterns in the data is not
learning; **applying your current rules to the items and counting how many match
Y** is. Every pass ends with a number — "rules v2 match 271 of 300; 29 misses" —
and you never write or change an instruction without having measured the rules
it contains. A pass whose rules you did not score did not happen.

Everything below is how to run that loop well. Batch size, how to check, when to
look back — choose them yourself from the data in front of you.

## Know what Y is before you start

Restate the target in one or two lines: what an item is, what the expected
outcome is for each item, and how you will tell whether an answer matches.
If the user gave only logic, the logic is your first hypothesis and the items
are what test it. If they gave only answers, the answers are the target and the
logic is what you are here to find. If something essential is missing — you
cannot tell what Y is for an item — ask once with `clarify`, then go.

## Starting from nothing is normal

Most agents start this with no instructions. Do not run the first batch blind:

- Turn whatever the user explained into first draft rules.
- Read a small, varied sample of the items (and `describe_tables` /
  `inspect_data` where they are rows) to fill the obvious gaps — what the
  fields mean, what Y looks like, what varies.
- Keep the first batches **small and varied**. Early on almost everything is a
  miss, and a big batch only repeats the same lesson.

First rules are guesses. Later batches should be free to rewrite or merge them,
not only to add on top of them — otherwise you finish with a pile of patches.
Already-existing instructions are just a head start: `search_instructions`
first and build on what is there.

## Keep a ledger so nothing is skipped

Your first action, before reading any item: `create_note` with the title
**exactly `Plan`**. Keep it current with `edit_note`. A `Plan` note with
unchecked `- [ ]` items keeps the run going when you would otherwise stop early,
so give it the loop's own steps, then check each off only when it is done:

```
Target: <what an item is, what Y is, how a match is decided>
- [ ] read every item (batches: …)
- [ ] score rules v1 against Y on all items
- [ ] learn from the misses; rescore until it stops improving
- [ ] write / edit the instructions
- [ ] final rescore of all items with the written instructions
```

Under the checklist keep the running match rate per pass and the rules in play
with the misses that motivated each one. Saying "I'll do that next" and ending
the turn is not a pass; if a step is unchecked, do it.

When there are many items, keep the per-item record somewhere sturdier than the
note — a saved table, or an Agent List (`create_list`, then `submit_<list>`)
with one row per item: its id, the expected Y, the agent's answer, `match`
(yes/no), and a short reason. Query it before each batch to pick up what has
not been read yet. "Learned from all" means every item has a row.

## Check each item the cheapest way that works

A match does not have to be a number. Per item, decide yes or no with a short
reason, by whatever fits:

- **Query** when Y and the items are data — compare in SQL/pandas over the whole
  set at once. Re-checking every item after a new rule is then one query, not
  one call per item; do that. Score the **rule set**, not one factor at a time:
  one `create_data` that encodes your current rules as the prediction
  (`CASE WHEN <rule 1> OR <rule 2> THEN 1 ELSE 0 END`), compares it with Y, and
  returns matched / false positives / false negatives plus the misses
  themselves. Per-factor rates help you find a rule; they are not the score.
- **Read once** when the items are files (`list_files`, `read_file`,
  `grep_files`). `grep_files` finds things across files but prints only part
  of a large result, so it is not a way to read them; read with `read_file`,
  issuing a whole batch of calls (10–20 files) in one response. Keep reading
  without stopping to narrate — between batches there is nothing to report
  yet. Pull out what the rules need into rows (or a compact table in
  the Plan note) the first time through, so later checks work from those rows.
  Re-read a file only when a new rule needs something you did not extract, or
  the extraction itself was the problem.
- **Judge** when Y is unstructured — a summary, an assessment, a decision with a
  rationale. Decide yes/no against Y with the reason in one line. Hold your
  standard steady while a batch runs; if the standard itself was wrong, fix it
  between batches and say so.

## Learn from the misses, not from each miss

After a batch, look at the misses together. Group them by cause — a wrong
definition, a missing exclusion, the wrong date, the wrong join, a reading
error, a judgment the agent keeps getting wrong.

When the misses do not separate on any raw column, the cause is usually in a
**derived** one: the hour, weekday or month inside a timestamp, a difference
between two fields, a length or count, a word in free text, a combination of
two values. Try those on the misses before concluding they are unexplained.
And compare the misses with the matches, not only with each other — a pattern
is what the misses have that the matches do not.

When two items say much the same thing but have different Y, the difference is
rarely the wording: look at everything else about them — header or metadata
fields (plan, tier, region, owner, source), who sent it, when, how much. A rule
keyed on exact phrases is usually a proxy for a field you have not looked at,
and it breaks on the next item worded differently. Then:

- One `create_instruction` or `edit_instruction` per cause, not per item.
  Prefer a few general rules over many narrow ones.
- **Write rules that decide.** Someone holding one item and your rule must
  reach the same yes/no you would. "High amounts raise the risk" decides
  nothing; "flag when claim_amount > 5000 and policy_age_days < 90" does. The
  cutoff is part of the rule — find it from the items (try a few, keep the one
  that matches Y best) and write it in. Where Y is a judgment, write the test a
  reader can apply ("urgent when it reports an outage affecting more than one
  user"), not the impression.
- A rule that only holds in combination says so: write the combination, not
  each factor as a vague "signal".
- A rule is supported by several items, ideally from different batches. One
  item is a data point.
- **Rules only, never record-level facts.** No item ids, no observed totals or
  rates, no names — those go in `evidence`. A cutoff that decides the outcome is
  not an observed fact; it is the rule. If a lesson only re-answers one item, it
  is an exception to record in the ledger, not an instruction.
- When a new lesson contradicts an existing rule, edit that rule. Never leave
  two standing.
- Cite the items that motivated a rule in `evidence`.

Write instructions as soon as a scored rule beats the previous pass — do not
ask permission first. Everything you write lands in a draft build that a person
reviews before it goes live; that review is the approval step, so asking in chat
only stalls the loop. Edit them as later passes improve them.

Then apply the updated instructions to the next batch. When a rule probably
fixes earlier misses, go back and re-check them — a rule that fixes 40 items and
breaks 10 should show up right away.

## Go wide when the items allow it

When the items are rows you can query together, one query scores all of them —
do that yourself; it is faster than any fan-out, and do not split hypothesis
tests over the same rows across sub-agents (by default sub-agents can read and
peek but cannot run tracked queries, so they come back blocked). Parallelism
pays when each item has to be read or judged (files, documents, free text).
When the items split into independent batches like that, run them in parallel
with `delegate` — one sub-agent per batch, all launched in one response. Put
everything a child needs in `goal` and `context`: what Y is, the current rules
verbatim, the item ids of its batch, and what to return.

Children **report, they do not write**: ask each for its per-item yes/no with
reasons and the rules it would propose, each with the items that support it.
You merge — deduplicate, resolve conflicts, keep what several batches agree on
— and only you write instructions. Parallel writers produce duplicates and
contradictions.

## Stop when it stops improving

Stop when every item has been read and one of these holds: everything matches,
the match rate has stopped rising across a pass, or the remaining misses are
exceptions no general rule should cover. **If you can name the next check, you
are not done** — "see whether the false positives share a field" is the next
pass, not a recommendation for the user. Run it.

Look at both kinds of miss each pass: false negatives point to a missing rule,
false positives to a rule that is too broad. When misses sit just past a
cutoff, move the cutoff and rescore rather than keeping a round number.

Before you finish:

- re-check the first items against the final instructions — they were judged
  before most rules existed;
- re-read every instruction: one that says "raise the risk", "is a signal" or
  "be careful" without deciding yes/no is not done — make it decide, or delete
  it and say why.

Ask the user only when you are stuck, not as a ritual step.

## Hand it over

Report briefly:

- the instructions written or edited (they land as a draft build for review),
  and what each one fixed;
- the match rate at the start and at the end, and how many items were read;
- what still does not match, grouped by cause, and the exceptions you chose not
  to turn into rules;
- any rule resting on an assumption the user has not confirmed.

Offer to lock the most important cases in as evals (see "Create eval cases") so
the next change tells you if it breaks them.
