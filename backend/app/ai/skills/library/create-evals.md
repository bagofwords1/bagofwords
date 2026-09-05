---
key: create-evals
title: Create eval cases
description: Use in training mode to turn a good answer into an eval case — confirm it with the user, then write a judge rubric that can actually fail.
category: general
version: "1.0"
modes: [training]
tags: [evals, quality]
---

An eval case is a promise that one question keeps having one right answer. Most
hand-written cases are worthless in one of two ways: they pin a number that
legitimately changes, or they carry a judge rubric so vague it passes anything
plausible. Both look green forever and catch nothing.

## 1. Start from a run that was actually right

The source of a case is a specific successful run — a question someone asked,
an answer someone accepted. `list_agent_executions` (filter by `prompt_search`,
`tool_name`, or feedback) is the history. Read the run before writing anything:
the tools it called, the filters and joins that made it correct, and the
definitions the user implicitly approved along the way.

Worth a case: metrics with a contested definition, anything feeding a decision,
questions asked repeatedly. Not worth one: vanity counts, one-off uploads, and
anything whose value moves hour to hour with no stable relationship.

## 2. Write the prompt as the user, not as yourself

`prompt.content` is the question **under test**, exactly as a user would type it
("What was net revenue in Q2 2026?"). Never the meta-instruction that got you
here — "create an eval that checks revenue" replays the meta-instruction and
tests nothing. The expected answer belongs in the rubric, not the prompt.

## 3. Assert method, then judgment

Two rule types, and they do different jobs:

- **`tool.calls`** — set-membership of the tools the run must use (it must call
  `create_data`; it must consult `read_instruction`). This is how you pin *how*
  the answer is reached, and it survives the numbers changing.
- **`judge`** — the rubric, for everything a matcher cannot express.

Do **not** add `field` rules. They assert on raw SQL and data, and they rot the
moment the schema drifts — a failing case nobody trusts gets muted, and then the
whole suite gets muted.

## 4. The judge rubric — four parts, nothing else

Ground it in the run you just read, and name all four:

1. **The output shape.** "A list of opportunities, one row per opp — not a count."
2. **The filters and joins that define correctness.** "Opps owned by the
   requesting user; joined to accounts via `account_id`; open stages only —
   exclude Closed-Won and Closed-Lost."
3. **The definitions the user approved.** "'My opps' means `owner_id` = the
   current user."
4. **One or two negative criteria** — plausible but wrong variants to reject.
   "Reject if it returns a count instead of a list." "Reject if it includes
   closed deals."

The negative criteria are what make the rubric able to fail. A rubric without
them passes anything that looks like an answer.

Never write tautologies — "reject if irrelevant", "reject if it misses the asked
metric" — they are unfalsifiable and give a green case that tests nothing. Do
not restate the user's question (it is already in the prompt) and do not list
the tools (the `tool.calls` rules cover that). Keep it tight: the judge reads
the rubric against a full trace, and every extra sentence is another thing it
can weigh wrongly.

## 5. Confirm with the user before it becomes a standard

A case is a rule the org will be held to, so put it to the user before you rely
on it — in plain words, not JSON: the question it replays, what it asserts, and
what it will reject. Ask specifically whether the definitions in part 3 are
right. This is the step that catches "actually we count that differently", which
is much cheaper to hear now than as a failing suite next month.

## 6. Prove it, then report

`run_eval`, then read the result with `get_eval_run`.

- **Passes** → keep it. You now know it reflects current behavior.
- **Fails** → you learned something before it mattered. Either the expectation
  is wrong (fix it) or the agent genuinely disagrees with the accepted answer —
  that is a finding to report, never a reason to loosen the rubric until it goes
  green.

A case that was never run is a note, not an eval. Report each case with its
first-run verdict, and separately list anything you could not reproduce, with
both values and your reading of why.
