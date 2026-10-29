# Feedback Loop — PPTX sandbox validator parity

Verifies the slides (python-pptx) sandbox hardening from #1316 (audit finding
"Authenticated RCE in slides PPTX sandbox via AST filter bypass in
create_artifact flow"). Before it, `PptxSecurityVisitor` blocked only the
enumerated `FORBIDDEN_ATTRIBUTES`, while the main `CodeSecurityVisitor` blocks
every private/dunder attribute (only `__name__` allowed). Dunders nobody listed
(`__reduce__`, `__base__`, `__getattribute__`, `__init_subclass__`,
`__traceback__`, ...) passed on the PPTX path. It also lacked the hardcoded-path
file-reader guard, and `pandas`/`numpy` are importable there.

Questions this loop answers:

1. Does the PPTX validator now reject everything the main one rejects?
2. Do legitimate decks still build: no false positives on the patterns the
   slides prompt teaches?
3. In the real UI, does a rejected deck fail cleanly, without writing a file,
   and with an error the planner can act on?

## Loop A — unit (no LLM, no services)

`backend/tests/unit/test_pptx_sandbox_validation.py`, through the public
surface (`validate_python_code`, `validate_pptx_code`,
`PptxCodeExecutor.execute_pptx_code`):

- Parity: each unlisted private/dunder attribute and each hardcoded-path read
  is rejected by **both** validators.
- A rejected deck raises `UnsafePythonError` and writes no `.pptx`.
- **Prompt-reference deck**: every pattern the slides prompt's quick
  reference teaches (16:9 setup, background fill, shapes, text frames, a chart
  with legend and data labels, a table, speaker notes, `image()` placement, the
  `pptx.enum.dml` import, `type(x).__name__`) validates and builds. If the
  prompt ever teaches something the validator rejects, this test fails before
  the model pays a repair round-trip on every deck.

```bash
cd backend && TESTING=true BOW_DATABASE_URL='sqlite:///db/app.db' \
  uv run pytest -q tests/unit/test_pptx_sandbox_validation.py \
  tests/unit/test_pptx_repair.py tests/unit/test_code_sandbox.py
```

Results: 83 passed together with `test_pptx_repair.py`,
`test_code_sandbox.py` and `test_sklearn_sandbox.py`. With the #1316 fix
reverted, 12 of the 13 new tests fail; only "ordinary deck still generates"
passes, as it should.

## Loop B — real stack, real UI, scripted LLM

The sandbox's Anthropic key had no credit, so the planner was driven by a
scripted OpenAI-compatible mock registered as a `custom` provider (same pattern
as `tools/agent/mock_llm_demo_data.py`). Its planner turn returns one native
`create_artifact(mode="slides", code=...)` call; any prompt containing "probe"
gets the same deck plus `el = slide._element`. Everything else is real:
backend, Nuxt UI driven by Playwright, the agent loop, `create_artifact`, the
PPTX executor (subprocess sandbox, the default since #1095), and LibreOffice
previews.

| Run | Prompt | UI | Disk | Planner sees |
|-----|--------|----|------|--------------|
| legit | "Build me a slide deck about Music Store revenue by genre." | "Presentation Created · Slides", artifact card | 2-slide `.pptx` (title band + text, bar chart) and 2 preview PNGs | `Created artifact ... Generated 2 slide preview images.` |
| probe | same, with the `_element` read | "Failed to Create Presentation", with the error inline | no new `.pptx` | `pptx_execution_failed: ... Forbidden attribute access: '_element'` plus remediation |

Both runs were repeated after rebasing onto #1095, with identical results. The
backend log shows the legit deck running in a sandboxed child
(`code sandbox: child ... mode=pptx landlock=applied tcp_blocked=True`). The
probe never reached a child: the AST validator stopped it first, so the two
layers are independent, not redundant.

Code the planner authors itself runs with `max_repairs=0`, so the error goes
straight back to the planner, not the in-tool repair loop. That's why the
planner's slides contract now carries the public-API rule.

## Prompt changes that came out of the loop

The slides prompt told the model to "draw a **translucent** scrim" behind text
on photos. python-pptx has no public transparency API, and the usual
workaround (`fill._xPr`, `shape._element` plus lxml) is exactly what the
hardened validator rejects. So:

- `create_artifact._build_slides_prompt` and the legacy slides edit prompt
  now ask for a solid band or panel and state "public python-pptx API only".
- `artifact_authoring._SLIDES_CONTRACT` (planner-authored decks) gets the
  same rule.
- `create_artifact._fix_pptx_code` (the in-tool repair prompt) explains a
  "Forbidden attribute access" error: rewrite with the public API, or drop the
  effect.

## Not covered

- A live frontier model writing deck code. Re-run Loop B with a real
  provider and grep the backend log for `Forbidden attribute access` to
  measure how often real decks hit the rule.
- The 11 `unit-tests` CI failures seen on #1316 are pre-existing on the base
  branch (reproduced locally without the change) and unrelated to the PPTX
  path.
