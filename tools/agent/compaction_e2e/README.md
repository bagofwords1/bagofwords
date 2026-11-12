# Context compaction — live UI loop

Drives the real UI as a user against a real LLM to verify the context budget
ladder and `/compact`. Report: `docs/feedback-loops/context-compaction.md`.

```bash
# stack: backend (uvicorn without --reload, BOW_ENCRYPTION_KEY pinned) + frontend dev
export OPENAI_API_KEY=...            # never commit it
cd tools/agent/compaction_e2e
ln -s /opt/node22/lib/node_modules node_modules   # global playwright
node 01_signup.mjs                    # sign up, skip onboarding
node 02_models.mjs                    # OpenAI provider, gpt-5.6-luna only
SALES_CSV=/path/sales_2025.csv node 03_first_turn.mjs
CTX=100000 node 04_set_context.mjs    # lower the model's context window in the UI
node 05_conversation.mjs              # long data-heavy conversation (prompts.json)
CTX=80000 node 04_set_context.mjs && PROMPTS=prompts2.json TAG=06 node 05_conversation.mjs
node 07_slash.mjs                     # /compact <focus>: menu, receipt, divider, summary
node 08_deferred.mjs                  # /compact typed during a run
node 11_recall.mjs                    # agent still knows compacted facts
```

Watch `[context] governor` and `[compaction]` lines in the backend log.
