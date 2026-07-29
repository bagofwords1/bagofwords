"""Shared prompt blocks used by multiple planner prompt builders.

Kept in a standalone module so the active builder (prompt_builder_v3) and the
deprecated v2 builder (which still serves the knowledge harness and the
BOW_PLANNER=v2 fallback) state identical contracts. Update the text here, and
every instruction-writing prompt (training mode, knowledge harness) picks it
up.
"""

# The anti-overfit contract for every prompt that can call
# create_instruction / edit_instruction. The generality gate in
# app/ai/instruction_quality.py enforces this server-side
# (rejected_reason='overfit'); this block exists so the model doesn't
# attempt overfit text in the first place and knows how to recover
# when the gate fires.
NO_OVERFIT_BLOCK = """NO OVERFIT — HARD RULE FOR INSTRUCTIONS
An instruction is a REUSABLE RULE: a definition, convention, or semantic that changes how FUTURE queries are written for OTHER users. It is never a fact about specific records.

Never write into instruction text:
- Specific record identifiers (row/invoice/order ids) — "exclude order 9174" is not a rule.
- Individual people or customers ("Maria's last name is Novak", "agent 7 is Anna").
- Observed counts, totals, or metric values ("there are 59 customers", "revenue is $84,230").
- Volatile data facts that go stale as data changes (row counts, date ranges, distributions).

Litmus test before EVERY create_instruction / edit_instruction call: would this instruction change the SQL for at least one plausible FUTURE question, asked by a DIFFERENT user, about DIFFERENT records? If it only re-answers today's question, do not write it.

When a learning mixes a general rule with record-level detail, capture ONLY the general rule and drop the detail:
- "order 9174 is a duplicate, exclude it" → capture an exclusion rule only if the user stated one (e.g. "exclude duplicate/cancelled orders"); never hardcode the id.
- "revenue means net of tax (I checked: $84,230)" → capture the definition; drop the observed number.
Numbers that are PART of a user's stated definition are fine ("enterprise customer = annual spend > $50k"); numbers OBSERVED in query results are not.

The tools enforce this: record-level text is rejected with rejected_reason='overfit'. On rejection, restate the text as the general rule (without the record-specific detail) or skip — never resubmit the same text."""
