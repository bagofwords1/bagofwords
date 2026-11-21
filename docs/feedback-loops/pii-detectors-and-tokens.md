# PII detectors and reversible tokens — feedback loop

Verifies three things on a running stack: names are found by a detector
(Presidio), a value marked "Hide from the model" reaches the LLM only as a
token and comes back as the real value for people and tools, and a detector
outage blocks the request. Design: `docs/design/pii-policies.md`.

## Run it

```bash
# backend with the detector: either the in-process extra …
cd backend && uv sync --frozen --extra dev --extra pii
# … or an analyzer service
python tools/agent/presidio_service.py --port 5003          # spaCy or GLiNER
export BOW_PII_PRESIDIO_URL=http://127.0.0.1:5003
export BOW_PII_PRESIDIO_TIMEOUT_SECONDS=120                 # GLiNER on CPU
export BOW_LLM_TRACE_FILE=/tmp/bow-agent/llm_trace.jsonl BOW_LLM_TRACE_FULL=1

uv run python ../tools/agent/pii_capture_llm.py &            # mock LLM
ANTHROPIC_KEY_LOOP=… uv run python ../tools/agent/pii_provider_exemption_setup.py --tokens
node tools/agent/pii_provider_exemption_ui.mjs ask out.png gpt-public \
  "Please email jane.roe@contoso.com the Q3 summary"
node tools/agent/pii_provider_exemption_ui.mjs ask out.png claude-haiku-5-5 \
  "What is the email address of the customer Frank Harris?"
python tools/agent/pii_detector_eval.py                      # precision / recall
```

`--tokens` sets the Default policy to hide emails and (with a detector)
person names from the model. The trace file is the ground truth for what was
sent to the provider.

## Results

| Check | Result |
|---|---|
| Mock: prompt with an email | Model received `<EMAIL_…>` only; chat shows the real address (`media/pr/pii-tokens/chat-mock-restored.png`) |
| Haiku 5.5: "Which customer has the email fharris@…" | 5 calls to Anthropic, 0 contain the address; the SQL ran with the real address; answer names the right customer (`chat-haiku-email.png`) |
| Haiku 5.5: "email of the customer Frank Harris", GLiNER service | 3 calls, 0 contain the address or the full name; answer shows name and address (`chat-haiku-name.png`) |
| Detector too slow / down, `on_detector_error: block` | Request blocked with "the PII detector is unavailable"; audit entry `pii_protection.detector_failed` |
| Instructions and schemas | Sent as written; the detector never runs on them |
| Automated | 127 unit + 30 e2e PII tests, incl. a differential test against the previous engine |

Settings page: `media/pr/pii-tokens/settings-en.png`, `settings-he.png`.

## Detector accuracy (`pii_detector_eval.py`, min score 0.5)

| Model | EN person P/R | EN location P/R | EN org P/R | False positives on text without PII | Hebrew | Latency / memory |
|---|---|---|---|---|---|---|
| spaCy `en_core_web_sm` (the `pii` extra) | 0.92 / 0.86 | 0.83 / 0.62 | 0.70 / 0.70 | 23 | none | ~13 ms, small |
| spaCy `en_core_web_lg` | 0.86 / 0.89 | 0.81 / 0.81 | 0.80 / 0.80 | 16 | none | ~1.1 GB |
| GLiNER multilingual (service only) | 1.00 / 1.00 | 1.00 / 0.81 | 0.83 / 1.00 | 10 (3 at 0.8) | person 1.00 / 0.92 | ~120 ms per short text, ~1.8 GB |

## What this loop found

- **GLiNER on CPU is slow on large prompts.** In this 4-CPU sandbox a 2–20k
  character chunk took 8–18 s, so the default 10 s timeout tripped and the
  request was blocked (correctly, per policy). Use a GPU or raise
  `BOW_PII_PRESIDIO_TIMEOUT_SECONDS`; spaCy does not have this problem.
- **Detection is best effort, pattern rules are not.** With the name split
  across `FirstName` / `LastName` columns, the first name was hidden but the
  surname alone ("Harris") reached the model in a result row and in generated
  SQL. spaCy `sm` also split "Frank Harris" and hid only "Frank". Emails
  (pattern rule) never leaked. Names in tabular data need column-level
  masking to be guaranteed; that is not part of this change.
- **False positives are harmless with tokens, not with replace.** GLiNER
  tagged the table name `Customer` as a person in replayed code; because the
  token is restored before code runs, the query still worked. With the
  `replace` action the same false positive would break the query.
- **The model sometimes talks about placeholders.** Haiku first refused to
  use them, then said a value was "hidden"; the instruction given to the
  model was tightened twice. It now answers with the value but may still
  mention a placeholder in its reasoning line.
- The mock LLM can echo the example token from that instruction
  (`<EMAIL_3fa9c21d>`) into a report title; no real value is behind it, so
  it is shown as is. Mock artifact only.
