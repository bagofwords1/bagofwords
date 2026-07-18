# Feedback Loop — "read eval result should show the full trace … it doesn't see the expectations/prompt at all"

When the agent woke on a failed eval run and called `get_eval_run`, it got back
per case only `{case_id, case_name, status, failure_reason}` — and
`failure_reason` is **null for a normal `fail`** (the evaluator persists
`failure_reason=None` and puts everything in `TestResult.result_json`). So the
agent saw the string `"fail"` and nothing else: not the prompt, not the
expectation rules, not the judge's reasoning. It then flailed ("ended in error
without detailing the failure reason…"). The run detail *page* showed all of it
because the page reads `result_json`; the tool never did.

## Root cause (validated)

- `get_eval_run` built each result from the `TestResult` row's `status` +
  `failure_reason` column only (`get_eval_run.py`), ignoring
  `TestResult.result_json` (`spec.rules`, `rule_results` — the per-rule verdicts
  with judge messages / expected-vs-actual) and `TestCase.prompt_json`.
- `failure_reason` is almost always null for `fail`: the evaluator
  (`TestEvaluationService`) records the substance in `rule_results`, not the
  column.

## Loop A — real data (validated on a production `result_json`)

A case with a `tool.calls` + `judge` rule that failed, read straight from the
sandbox DB and passed through the new view helpers
(`app/ai/tools/eval_result_view.py`):

```
CASE: Genres broken down by media type  | status: fail | failure_reason col: None
  -> prompt: How many genres are in the database? Answer with a single number.
  -> derived failure_reason: create_data calls=0, expected min=1, max=None | Judge evaluation failed
  -> rules: [tool.calls: create_data min 1, judge: The answer MUST be a table with one row per genre …]
  -> rule_results: [
       {rule: "tool.calls: create_data min 1", status: fail, message: "create_data calls=0, expected min=1", actual: "0"},
       {rule: "judge: …", status: fail, message: "<judge reasoning>", actual: "False"}]
```

**Before** this change the same read returned `status: "fail",
failure_reason: null` and nothing else.

Judge reasoning: `rule_results_view` surfaces the rule's `message`, falling back
to `evidence.reasoning` — exactly where the evaluator writes the judge's
free-text verdict (e.g. the Hebrew "לא ביצע פילוח לפי SH_SHUMOT…" from the
reported run). When the judge produces rich reasoning, it appears verbatim
(truncated to 400 chars); when the underlying agent run itself errored (so the
judge had no answer to grade), the message is the evaluator's generic string —
faithfully reflecting what was stored.

## The change

- New `app/ai/tools/eval_result_view.py`: `rules_view`, `rule_results_view`
  (failing rules first), `derive_failure_reason` (synthesize from failing rules
  when the column is null), `prompt_text`.
- `get_eval_run` now returns `EvalCaseDetail` per case: `prompt`, `rules`,
  `rule_results`, a derived `failure_reason`, and — with the new
  `include_transcript=true` input — the agent transcript for failed cases
  (bounded, via the existing `get_result_transcript`). The observation summary
  leads with the first failing case's reason so the conversation-history digest
  is actionable.
- `run_eval`'s inline results reuse `derive_failure_reason` for consistency.

## Verification

- `backend/tests/unit/test_eval_result_view.py` — 9 tests over the transforms
  (rule summaries, failing-first ordering, evidence.reasoning fallback, derived
  reason, truncation). Full eval unit set: 36 passed.
- In-process end-to-end: ran a failing case, invoked `GetEvalRunTool.run_stream`
  with `include_transcript=true` — output carried `prompt`, `rules`,
  `rule_results`, derived `failure_reason`, and a transcript; `GetEvalRunOutput`
  validated.

## Notes

- Backend-only; no new UI surface (the chat card already renders
  `failure_reason`, which is now populated). The run detail page was already
  correct — it read `result_json` all along.
- Live re-run with a fresh judge answer was blocked by Anthropic API credit
  exhaustion in the sandbox (`credit balance is too low`); the transform is
  validated against real stored `result_json` instead.
