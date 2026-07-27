# Feedback Loop — "it is not redacting … only after refresh"

With PII protection enabled, a message the user types containing PII (e.g.
`show me my email yochze@gmail.com`) rendered with the **raw** value in the live
chat and only flipped to `[REDACTED_EMAIL]` after a page refresh. This validates
that the streamed prompt is masked live, with no reload.

## Root cause (validated)
The report view renders the user's message **optimistically** from the text it
just sent, before any server round-trip:
`frontend/pages/reports/[id]/index.vue:4034-4041` pushes
`{ prompt: { content: text } }` with the raw text and binds it at
`:220-226` via `<InstructionText :text="m.prompt.content" />`.

Persisted rows are masked by the display serializers (StepSchema field
serializer + the `X-Organization-Id` middleware ContextVar), which is why a
**reload** showed the redacted value. But the optimistic bubble never passes
through a serializer, and the SSE stream — the app uses SSE, not WebSockets —
did not carry a redacted prompt to correct it. So the raw value sat on screen
until `loadCompletions()` replaced the list wholesale on reload
(`index.vue:3193-3198`).

The AI response itself was never at risk: the model is fed the redacted prompt
at the LLM chokepoint, so its echo of the request already shows `[REDACTED_…]`.
The gap was purely the client-side optimistic bubble.

## The fix
Close the gap on the existing first stream event instead of adding a protocol:

- **Backend** — `backend/app/services/completion_service.py`: before the SSE
  generator, compute the display-redacted prompt via
  `load_redactor_for_org(org, async_session_maker).redact_display(...)` and emit
  it as `user_prompt` in the `completion.started` event (previously the raw
  `completion_data.prompt.content`). Loaded on its own session, so it is safe
  after the request DB session is released. Enterprise-gated and a no-op when
  the feature is off (`load_redactor_for_org` returns `None`).
- **Frontend** — `frontend/pages/reports/[id]/index.vue`: in the
  `completion.started` handler, patch the optimistic user bubble
  (`messages.value[sysMessageIndex - 1]`) `.prompt.content` to the redacted
  `payload.user_prompt`. The `InstructionText` binding updates in place, so the
  bubble flips live without touching the send/stream/reconcile flow.

## Loop — live confirmation (real LLM)
Requires the running stack + an Anthropic key configured as the org model.

```bash
tools/agent/boot_stack.sh --dev
cd backend && BOW_DATABASE_URL=sqlite:///db/agent.db uv run python ../tools/agent/seed_org.py
ANTHROPIC_API_KEY=… uv run python ../tools/agent/setup_haiku_llm.py
# enable PII protection with an email rule (Settings → PII Protection, or the API)
cp ../tools/agent/verify_pii_live_sse.mjs ./_v.mjs -t ../frontend/  # run from frontend/
cd ../frontend && node ../tools/agent/verify_pii_live_sse.mjs
```

Observed output (PASS): the user bubble is `show me my email yochze@gmail.com`
at t≈0, then `show me my email [REDACTED_EMAIL]` by t≈500ms — **while the URL is
unchanged and the agent is still "Thinking"**, i.e. mid-stream, no reload. The
final page contains `[REDACTED_EMAIL]` and never the raw address.

## What this proves / regression notes
The redaction is visible live in the actual chat bubble via the SSE stream, not
only after a refresh, and the streamed/persisted views stay consistent. Stored
data is untouched (masking is display-only), so analysis, step reuse and exports
still operate on the real values. When PII protection is off or the instance is
unlicensed, `user_prompt` carries the raw text exactly as before.
