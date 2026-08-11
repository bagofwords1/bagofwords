# Feedback Loop — reading one artifact hydrates the full report graph

On artifact-heavy reports, active-artifact discovery and the `read_artifact`
and `edit_artifact` tools took tens of seconds even when the requested artifact
payload was small. The validated claim is that loading one Artifact implicitly
loaded its owning Report and all mapper-level `selectin` collections.

## Root cause (validated)

`Artifact.report` uses `lazy="selectin"`, while Report collections such as
artifacts, completions, queries, visualizations, and widgets also use
`lazy="selectin"`. Bare Artifact selects in `agent_v2.py`, `read_artifact.py`,
and `edit_artifact.py` therefore expanded into the complete report graph.
Visualization profile queries could re-enter the same graph through
`Visualization.report` and `Query.report`.

## Loop A — deterministic reproduction

The regression seeds one requested artifact plus unrelated sibling artifacts,
records SQL while exercising all three paths, and asserts that the requested
content still returns without any `FROM reports` query.

```bash
cd backend
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db \
  .venv/bin/pytest tests/unit/test_artifact_relationship_loading.py -q
```

Before the fix:

```text
3 failed
AssertionError: loading one artifact hydrated its owning report and the report's collections
```

## The fix

- Active-artifact, read-artifact, and edit-artifact queries apply
  `lazyload("*")` to the Artifact entity.
- Visualization profile queries suppress mapper defaults and explicitly load
  only `Visualization.query`, `Query.default_step`, and `Query.steps`, with
  nested relationship cascades disabled.

After the fix:

```text
3 passed
```

Related artifact verification:

```bash
cd backend
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db .venv/bin/pytest \
  tests/unit/test_artifact_relationship_loading.py \
  tests/unit/test_read_artifact_windowing.py \
  tests/unit/test_artifact_feedback_loop.py -q
```

Observed: `36 passed`.

## What this proves / regression notes

Artifact discovery and tool reads are now proportional to the explicitly
requested artifact and visualization profiles, not to the total number or size
of objects retained by the report. Completion-service Report fetches and the
message-history tool-digest N+1 remain separate optimization targets.
