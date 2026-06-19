# Sandbox Feedback Loop — clarify threshold by agent `publish_status`

Validates the planner-prompt change that ties how readily the agent reaches for
the **clarify** tool to the data source's (agent's) **publishing status**:

- **`draft`** (agent still being built — "dev/training") → clarify **freely**,
  the same strict behavior as today. Surfacing ambiguity here is how a builder
  captures definitions as instructions.
- **`published`** (agent live in production) → **prefer common sense**. Resolve
  ordinary ambiguity (scope, time window, granularity, a term with one sensible
  schema mapping) by assuming the most reasonable interpretation, stating it in
  one line, and proceeding. Clarify **only** for genuine blockers.

The agent's status is already rendered into the schema context as a per-source
`<status>` block (PR 390, `TablesSchemaContext._render_status_xml`). This change
makes `PromptBuilderV3._build_system` *act* on it instead of only describing it.

---

## The change (PR 390 base)

`backend/app/ai/agents/planner/prompt_builder_v3.py`, `_build_system`:

1. The clarify protocol now **leads with the `<status>` gate** — published =
   prefer common sense; draft = clarify freely — and reframes the strict
   "when to call clarify" list as the **draft** posture (plus the rare published
   blocker).
2. The schema-directives line for `<status>` is rewritten to set the clarify
   threshold (draft strict / published common-sense / disabled don't-rely)
   instead of only describing the status.
3. The worked "active users" example is made status-aware (draft → clarify;
   published → assume "last 30 days", state it, `create_data`).

No new plumbing: `publish_status` already reaches the prompt via the schema
section, so the model reads it per source.

---

## Environment setup (fresh sandbox)

App targets **Python 3.12**.

```bash
cd backend
python3.12 -m venv /tmp/venv312
/tmp/venv312/bin/pip install -q --upgrade pip

# Heavy/native DB drivers aren't needed for this prompt-logic loop.
grep -ivE '^psycopg2|^pyspark|^thrift|^pyodbc|^grpcio-tools|^confluent-kafka|^snowflake|^cx[-_]Oracle|^oracledb|^pymssql|^sqlalchemy-bigquery|^google-cloud' \
  requirements_versioned.txt > /tmp/reqs_lite.txt
/tmp/venv312/bin/pip install -q -r /tmp/reqs_lite.txt

export BOW_DATABASE_URL="sqlite:///db/app.db"   # required by bow-config.dev.yaml
mkdir -p db
```

---

## Loop — live Haiku confirmation

The harness builds the **real** v3 system prompt (`PromptBuilderV3.build`) with a
**real** rendered `<status>` block (`TablesSchemaContext.render_combined`) over a
`users` table that has **no unambiguous "active" signal** (`last_login_at`,
`plan`, `created_at`…), then asks an Anthropic **Haiku** model to plan one turn.
We classify each response as **CLARIFY** (the `clarify` tool, _or_ a plain-text
question — draft sometimes asks in prose) or **PROCEED** (a build/research tool
like `create_data` / `inspect_data`, or a text answer with no question).

Secrets via **env vars only — never commit them**:

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
export ANTHROPIC_API_KEY=...                       # never commit
# optional: export HAIKU_MODEL=claude-haiku-4-5-20251001   (default)
#           export REPEATS=2

/tmp/venv312/bin/python scripts/clarify_status_feedback_loop.py
```

Two assertion classes (both must pass):

- **Class A — ordinary ambiguity** (one sensible mapping, fuzzy scope): the
  draft agent clarifies, the published agent proceeds. e.g. *"How many users
  have logged in?"*, *"Count users who have logged in."*,
  *"How many accounts have a login?"*.
- **Class B — hard blockers** (a core term with several materially different
  meanings, no schema/instruction hint): **both** clarify — published relaxes
  for ordinary ambiguity, it does **not** recklessly guess undefined business
  terms. e.g. *"How many active users do we have?"*, *"Show me our power users."*.

**Observed (PASS, claude-haiku-4-5-20251001, temperature=0, REPEATS=3):**

```
== Class A: ordinary ambiguity — draft clarifies, published proceeds ==
[PASS] "How many users have logged in?"   draft=[CLARIFY,CLARIFY,CLARIFY]  published=[PROCEED,PROCEED,PROCEED]
[PASS] "Count users who have logged in."  draft=[CLARIFY,CLARIFY,CLARIFY]  published=[PROCEED,PROCEED,PROCEED]
[PASS] "How many accounts have a login?"  draft=[CLARIFY,CLARIFY,CLARIFY]  published=[PROCEED,PROCEED,PROCEED]

== Class B: hard blockers — BOTH clarify (published is not reckless) ==
[PASS] "How many active users do we have?"  draft=[CLARIFY,CLARIFY,CLARIFY] published=[CLARIFY,CLARIFY,CLARIFY]
[PASS] "Show me our power users."           draft=[CLARIFY,CLARIFY,CLARIFY] published=[CLARIFY,CLARIFY,CLARIFY]

RESULT: ALL PASS
```

### Iterating

Edit the protocol wording in `prompt_builder_v3._build_system` and re-run. If a
*published* Class-A case still clarifies, the override is being out-voted by the
strict directives — strengthen/raise the status gate. If a *draft* Class-A case
stops clarifying, the strict list got too weak. Class B guards the other rail:
if published stops clarifying a hard blocker, the relaxation went too far.

> Note on scenario choice: terms like "active user" / "power user" are genuine
> multi-meaning blockers — even a minimal "don't clarify" prompt can't (and
> shouldn't) stop Haiku clarifying them. They belong in Class B, not Class A.
> Class A must sit in the *ordinary-ambiguity* zone to discriminate the postures.

---

## Unit coverage

The descriptive `<status>` rendering is covered by
`backend/tests/unit/test_schema_section_agent_status.py` (PR 390), still green
after this change. The behavioral coupling is validated by the live loop above.
