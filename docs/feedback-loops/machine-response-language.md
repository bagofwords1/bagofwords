# Feedback loop — sub-agent follow-ups switch a Hebrew conversation to English layout

Automatic follow-ups must retain their originating response language, even when their internal instructions and tool results are English. The language must be present when the visible reply is created, not assigned after it starts rendering.

## Root cause

`backend/app/services/sub_agent_service.py::_fan_in_prompt` creates an English machine instruction. `backend/app/ai/agent_v2.py::_resolve_response_language` previously classified the head prompt without distinguishing machine triggers from human requests. A classifier could select English and persist it as the response locale, which also controls frontend direction.

## Fix and contract

- `backend/app/services/response_language_context.py` resolves an explicitly linked parent within the same report. The parent's persisted `response_locale` is the snapshot; the wave already retains its immutable parent completion ID, so no additional wave column or migration is needed.
- Sub-agent fan-in, waits, and check-ins pass their source completion ID. Other machine callers inherit the latest visible response. Hidden child completions do not select the conversation language.
- Legacy origins use the original human request, never a generated trigger. The caller resolves legacy language before creating the visible completion. Organization locale is the final fallback.
- `machine_turn.py` carries the resolved language on the event and hidden head. `completion_service.py` seeds it on the visible reply before persistence/broadcast. Queued continuations use the same metadata helper.
- The agent reuses saved language on replay and emits `completion.language` even for a pre-seeded reply. Actual human turns can still request another language.
- The response schema preserves the locale instead of dropping it during serialization.

## Deterministic loop

From `backend` with the project's Python 3.13 environment:

```sh
.venv/bin/python -m pytest tests/unit/test_response_language.py tests/e2e/test_machine_response_language.py tests/e2e/test_sub_agents.py -q --disable-warnings --db=sqlite
```

The e2e cases seed users, organizations, and reports through existing API fixtures, and use database rows only to represent historical/legacy completions and a newer intervening English turn. Only the LLM provider boundary is stubbed. The real machine-turn, schema, persistence, and agent paths run.

Assertions cover Hebrew and Arabic, saved and legacy origin metadata, delayed results after a newer English turn, the event locale, the reply's locale at initial insertion, and the completed reply locale. Unit cases also cover Spanish inheritance, machine trigger kinds, explicit user language changes, provider failures, and serialization.

Initial new contract tests failed (11 failures, 15 existing tests passed). After implementation the combined suite passed. A negative control forcing the machine context locale to English produced four e2e assertion failures for Hebrew/Arabic; the temporary override was then removed. This demonstrates that the tests detect the reported wrong-language state, rather than merely exercising code.

## Limits

No live-provider or browser screenshot verification was performed. These tests verify the language/direction metadata consumed by the UI, not the linguistic quality of a real model's output. No UI rendering code was changed. SQLite is covered; PostgreSQL was not run locally. No claim is made that every historical conversation already has recoverable language metadata.
