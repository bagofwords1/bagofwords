"""The single authoring spec for AI-written instructions.

Three paths author instructions and had drifted apart: the knowledge
harness prompt (``prompt_builder._build_knowledge_prompt``), the v3
training-mode block (``prompt_builder_v3``), and the create/edit tool
schemas. Each carried its own, partly contradictory idea of what an
instruction is. This module holds the one definition they all import.

The failure this exists to prevent is *episode-shaped* authoring: writing
an instruction as a log of the conversation ("do not use col1, it was
removed", "updated to reflect — previously said X") rather than as a
statement of current state. See ``tests/evals/test_episode_benchmark.py``
for the objective measurement, and ``app/ai/instruction_quality.py`` for
the server-side gate that rejects the worst cases at the tool boundary.

Keep this block short. It is injected into every authoring prompt, so
every sentence costs tokens on every capture turn.
"""

# The full spec — injected into the knowledge-harness and training prompts.
INSTRUCTION_SPEC = """WHAT AN INSTRUCTION IS
An instruction is one durable rule in this organization's semantic model. Its reader is an agent that already has the table schemas and does NOT yet know what question it will be asked. Write for that reader, not for the person you just spoke to.

STATE, NOT EVENTS
State what is true now. Never write about what changed, what was removed, what an instruction used to say, when something was learned, or what was just asked.
- Wrong: "Do not use col1, it was removed." — Right: the rule simply stops mentioning col1.
- Wrong: "Updated to reflect the clarified definition — previously said X, now Y." — Right: the rule states Y.
If a rule's subject stopped existing, delete that clause; never negate it. A tombstone ("X was removed", "X is deprecated") can never change a query and never expires — it is pure cost.
Why a change was made belongs in `evidence`, never in the instruction body. Version history already records what changed.

SAY ONLY WHAT THE SCHEMA CANNOT
The reader is already given table and column names and types. Do not restate the schema, and do not assert what does or does not exist — the schema is the authority on existence. Do write what the schema cannot express: what a term means, units and encodings (cents vs dollars), enum semantics, which of several plausible join paths is correct, which rows to exclude, how a metric is defined.

ALTITUDE — GENERALIZE BEFORE YOU WRITE
The session is evidence, not the subject. Before writing, name the concept the learning is about, then name two DIFFERENT future questions the rule would change the answer to — neither of them the question just asked. If you cannot name two, it is a session note, not a rule: do not write it.

FORM
One rule per instruction, titled with the concept it governs. Aim for 40-120 words; a rule that needs more than ~200 is usually two rules. No preamble, no restating the question, no reference to "this session"."""


# Compact variant for the tool-schema `text` field description, where the
# full spec would dwarf the rest of the schema.
INSTRUCTION_SPEC_BRIEF = (
    "State what is true NOW, as a durable rule for a reader who has the schemas "
    "but not yet the question. Never describe what changed, was removed, or used "
    "to be true — if a subject stopped existing, delete the clause rather than "
    "negating it ('do not use col1, it was removed' is always wrong). Never "
    "restate the schema or assert what exists. Do capture meaning, units, enum "
    "semantics, join paths, exclusions and metric definitions. One rule per "
    "instruction, 40-120 words. Change rationale goes in `evidence`, not here."
)


# Appended to edit_instruction guidance: removal is a normal, expected edit.
EDIT_REMOVAL_NOTE = (
    "An edit replaces the text with how it should read NOW. Removing content is a "
    "normal edit: when the user says something no longer applies or no longer "
    "exists, delete that clause and leave the rest intact — do not append a "
    "sentence saying it was removed, and do not rewrite unrelated clauses."
)
