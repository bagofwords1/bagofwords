# Internal context: what Bag of Words (BOW) already has (from the repo, Oct 2026)

Source: repository bagofwords1/bagofwords, VERSION 0.0.584. Not web research; facts read from the codebase.

## Product (README)
- Open-source "agentic analytics platform": connects any LLM to data, each agent gets its own data, tools, credentials, instructions, permissions.
- Surfaces: chat, reports, dashboards, automations, scheduled tasks, Slack/Teams/WhatsApp/email/Excel, and MCP clients (Claude Code, Codex) through an MCP gateway.
- Claims: query acceleration cache (10-1000x), deep / root cause analysis, governance (RBAC, approvals, audit, SSO, RLS), auto model routing, LLM-fallback.
- Built-in evals: eval sets ("test suites/cases") with expectation rules (tool calls, ordering, field rules, LLM judge), TestRun ties to InstructionBuild ("CI for agents"). Self-improving loop: failing eval -> agent drafts instruction fix -> re-run -> promote or pend approval. LLM-as-judge scores for accuracy, instruction coverage, context use in monitoring.

## Existing eval assets in repo
- `backend/tests/evals/spider/spider_eval.py`: runs Spider 1.0 questions through the real product (one SQLite data source per db_id), scores by execution match. Existing result file `spider_results.jsonl`: 444 questions over 9 dbs, exec_match 158/444 = 35.6%, 42 with no predicted SQL, 9 errors, mean 66.5 s/question. Likely an old/unfinished run (many null pred_sql), i.e. NOT a publishable number; also Spider 1.0 is saturated and the gold SQL often mismatches the agent's richer answers (exec-match brittleness).
- `backend/tests/evals/suites/*.yaml`: 12 YAML "Sanity" suites (smoke, dashboards, clarify, knowledge harness, judge, maturity gating, multi-turn dashboard, notes gate, scheduled tasks, training mode, dashboard reuse, comprehensive dashboard), run via pytest through the HTTP import endpoint. Multi-turn supported in YAML.
- `backend/tests/evals/benchmarks/overfit_suite.yaml` + `test_overfit_benchmark.py`: an objective "instruction overfitting" benchmark for the knowledge/self-learning harness: 4 bait cases (record-level facts that must NOT be persisted as instructions) + 2 control cases (reusable rules that MUST be captured), scored by regex over DB rows, fresh org per trial, BENCH_TRIALS configurable. Novel: no public benchmark measures this.
- `backend/tests/evals/artifact_iteration/`: 3 scenarios (12-turn memory gauntlet, complex one-shot dashboard, 8-turn destructive gauntlet) with an architecture-neutral scorer; before/after result: mean 0.801 -> 0.952 with Claude 4.5 Haiku, n=1 per cell (directional only).
- `docs/design/query-acceleration-benchmarks.md`: measured BigQuery (213M rows) and Snowflake (60M rows) live vs cached: scan reduction 3.3x / 6.0x, latency reduction 886x / 2126x, break-even 1.8 / 1.0 questions.
- `backend/loadtest/FINDINGS.md` exists (load testing).
- Skills library includes `create-evals.md`, `train-agent.md`, `audit-instructions.md`, `demo-data.md`, `infrastructure-rca.md`, `migrate-bi-dashboard.md`, `complex-dashboard.md`.
- Demo data generator and "Chinook Music Store" fixture used across evals.

## Implications for launch evals
- A public-benchmark run (Spider 2.0 / BIRD-style) must be re-done cleanly; the current Spider 1.0 jsonl is not usable as-is.
- BOW's differentiators that no public benchmark covers and that in-house harnesses already partly measure: multi-turn dashboard iteration memory, instruction overfitting/generalization in self-learning, eval-driven self-improvement (instructions as commits), query acceleration cost/latency, governance (RLS/RBAC) correctness.
- The product's MCP gateway makes a "Claude Code / Codex alone vs Claude Code / Codex + BOW MCP" comparison natural and ToS-friendly (comparing harnesses on the same public model, not benchmarking a competitor's SaaS).
