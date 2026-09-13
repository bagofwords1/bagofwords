# Feedback loop — role-scoped code visibility

Verified against a live sandbox: real backend, real Nuxt frontend, real LLM
(Claude 4.5 Haiku), real browser, real SQLite data source (the `chinook` demo).

## Setup

| Piece | Value |
|---|---|
| Users | `admin@bow.example.com` (system `admin`), `viewer@bow.example.com` |
| Restricted role | "Analyst (no code)" — reports + files + members, **no** `view_code`, **no** `run_custom_code` |
| Agent | Music Store (SQLite Chinook demo) |
| Question asked | "Which 5 artists have the most albums?" |

The viewer's `member` role assignment was removed, leaving only the restricted
role. That step is not incidental: RBAC unions permissions across roles, so
*adding* a restricted role next to `member` grants strictly more, never less.

## What the comparison showed

Same report, same completion, same endpoints, two callers:

```
VIEWER  code-bearing paths: 0
ADMIN   code-bearing paths: 3
         .…tool_execution.sub_timings_json.queries[0].sql
         .…tool_execution.result_json.query_timings[0].sql
         .…tool_execution.created_step.code
```

Both directions matter. Zero for the viewer is the feature; three for the admin
is the proof the redaction is not simply breaking code for everybody — the
failure mode a one-sided test would have missed.

Write path, live:

| Call | Viewer | Admin |
|---|---|---|
| `POST /queries/{id}/run` (builder, custom code) | **403** | 200 |
| `POST /queries/{id}/preview` (custom code) | **403** | 200 |

UI, same report:

| | Admin | Viewer |
|---|---|---|
| Chart / Data tabs | ✅ | ✅ |
| **Code** tab | ✅ | **absent** |
| **Edit** buttons | ✅ | **absent** |
| Answer + chart | ✅ | ✅ |

## The bug this loop caught

A leak that no unit test would have found, because it was in a key nobody had
thought to put on the allowlist.

After the first implementation the viewer's payload still contained the full
statement:

```
.completions[1].completion_blocks[0].tool_execution.result_json.query_timings[0].sql
  "\n    SELECT \n        a.Name AS artist_name,\n        COUNT(al.AlbumId) …"
```

`code`, `errors` and `executed_queries` were all correctly redacted. Per-query
*timing telemetry* carried the SQL one field over, in **two** containers with
the same shape — `result_json.query_timings[]` and
`sub_timings_json.queries[]`. The first fix covered only the second container
and the leak survived; the live diff caught that too.

Fixed by redacting the statement while keeping the measurements (`query_ms`,
`rows`, `result_bytes`) — how long a query took is not code, and a viewer who
may not read it is still entitled to know it ran.

Two regression tests landed: one per container, plus a whole-payload assertion
that no code string survives anywhere in a redacted result. The last one is the
one that would have caught this class in the first place, rather than the
specific key.

## Sandbox notes (for the next person)

- `.test` is a reserved TLD — `fastapi-mail` rejects it at registration, so
  sandbox users need `.example.com`.
- The sign-up page reads the invite as `?token=`, not `?invite_token=`.
- Uvicorn's `--reload` hung after serving chats (the known
  "Waiting for background tasks" stall); `fuser -k 8000/tcp` and restart, never
  touching the WAL.

---

# Follow-up loop — the prompt constraint

The redaction covers structured fields. It cannot reach the assistant's prose,
where the model can simply write the SQL into its answer. A system-prompt
constraint was added for that, and measured.

## What was measured

Question asked as the restricted user, deliberately adversarial:

> "How many invoices are there, and what query did you use to find out?"

Ground truth for what the model actually received came from
`BOW_PLANNER_DUMP_FILE` (the planner's own diagnostic dump), not from reading
the builder — which is the point of that env var.

| Attempt | Placement | Result |
|---|---|---|
| 1 | `<code_visibility>` in `_build_user_message` | **Never rendered.** The live path is the transcript bridge, which builds context in `_build_static_context` and never calls `_build_user_message`. |
| 2 | Rendered on both context paths | In the prompt. Model answered with the SQL anyway. |
| 3 | `ORG CONSTRAINTS` in the system prompt | In the prompt (confirmed in the dump). Model answered with the SQL anyway. |
| 4 | Same, reworded to give a sanctioned refusal | In the prompt. Model answered with the SQL anyway. |

Every attempt produced: *"The query I used was: SELECT COUNT(*) as
total_invoices FROM Invoice"*.

## Conclusions

**The constraint reaches the model and the model does not obey it** on Claude
4.5 Haiku when asked directly. It is kept, because it reduces code the model
*volunteers*, which is the common case. It is not a control, and the code says
so where someone changing it will read it.

**A prompt hint cannot be the boundary.** The payload redaction is, and it is
unaffected by any of this — the tool results, step code, timings and stream stay
withheld regardless of what the model writes in prose. What leaks here is the
model *retyping* SQL it saw during its own run.

If prose has to be airtight, the options are a deterministic post-filter on the
answer text (risks mangling legitimate answers) or verifying on a larger model.
Both are out of scope for this change and neither should be assumed to work
without the same kind of measurement.

## A pre-existing bug found on the way

Attempt 1 failed because the transcript path skips `_build_user_message`. The
org's **`<data_visibility>` block lives in that same dead spot** — so when an
organization turns off "Allow LLM to see data", the planner is never told. The
tools still withhold rows (that part is real), but the explanatory block that
stops the model retrying to "see" the data does not render on the live path.
Not fixed here; flagged as its own issue.
