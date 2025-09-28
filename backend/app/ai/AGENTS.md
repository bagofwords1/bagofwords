# AGENTS Guidelines for AI layer

### Purpose
Concise overview of `@backend/app/ai/` with emphasis on orchestration (`agent_v2.py`), the planner (`planner_v2.py`), tools, and how components fit together.

### Structure
- **ai/**
  - `agent_v2.py`: Primary orchestrator. Streams planning decisions, runs tools with retry/timeout policies, persists snapshots, and emits SSE/WebSocket events.
  - `agent.py`: Legacy orchestrator kept for backward compatibility.
  - `agents/`: Domain agents (e.g., planner, judge, reporter, suggest_instructions).
    - `planner/`: `planner_v2.py`, prompt builder, state, and related schemas.
    - `judge/`, `reporter/`, `suggest_instructions/`: Specialized sub-agents.
  - `context/`: `ContextHub` and builders for instructions, messages, resources, observations.
  - `tools/`: Tool base classes, metadata, utilities, schemas, and implementations.
  - `runner/`: `ToolRunner` and execution policies (`RetryPolicy`, `TimeoutPolicy`).
  - `llm/`: LLM providers and a unified `LLM` wrapper.
  - `code_execution/`: Facilities for executing/generated code (used by code tools).
  - `utils/`: Shared utilities (e.g., token counting).
  - `registry.py`: `ToolRegistry` and catalog filtering.
  - `prompt_formatters.py`: Prompt helpers used across agents.
  - `schemas/`: Pydantic models for planner, events, and AI-specific DTOs.

### Data flow (high level)
`agent_v2.AgentV2` → builds `PlannerInput` from `ContextHub` → `PlannerV2` streams decisions → if action: resolve tool via `ToolRegistry` → execute with `ToolRunner` (retry/timeout) → persist context snapshots/blocks → emit SSE/WebSocket events → optional post-analysis (judge, suggestions) → finalize execution.

### Agent loop (in `agent_v2.py`)
- Initializes: `ContextHub`, `ToolRegistry`, `PlannerV2` (with tool catalog), `ToolRunner` (retry/timeout), `Reporter`, `Judge`, `SuggestInstructions`, SSE queue, and WebSocket handler.
- Starts an agent execution and saves an initial context snapshot (schemas/files/messages/resources/instructions).
- Enters up to N planning iterations:
  - Refreshes warm context and saves a pre-tool snapshot.
  - Builds a typed `PlannerInput` (instructions, history summary, messages, resources, files, schemas excerpt) and kicks off background early scoring.
  - Pre-creates a planning block and streams partial decision updates; throttles token deltas for reasoning/content.
  - On final decision:
    - If `analysis_complete`: emit final answer, optionally run `SuggestInstructions` based on heuristics/history.
    - Else if `plan_type == action`: validate tool availability for the plan, resolve via `ToolRegistry`.
  - Executes the selected tool with a rich runtime context (db/org/report/completions/context view, ds clients, files), streaming `tool.progress` events.
  - Captures observation/result, updates steps/visualizations when relevant, persists snapshots, emits `tool.finished`, and rebuilds the transcript blocks.
  - Circuit breakers: invalid planner output retries, per-tool failure counters, and infinite-success loop guards.
- After the loop: final snapshot, optional title generation via `Reporter`, late scoring via `Judge`, and graceful finish/error handling.

### Planner (`agents/planner/planner_v2.py`)
- Single-action planner that streams tokens and incremental decisions.
- Components: `LLM` wrapper, `PromptBuilder`, `PlannerState`, partial JSON parsing, metrics (`first_token_ms`, `thinking_ms`, token usage).
- Emits typed events: `planner.tokens`, `planner.decision.partial`, `planner.decision.final`.
- Resilience: ignores empty/heartbeat chunks and tolerates partial/invalid JSON while constructing a valid `PlannerDecision` with structured errors and defaults.

### Tools system (`tools/` + `registry.py` + `runner/`)
- **Definitions**: Tools define a clear input schema and return an observation plus optional result payload.
- **Metadata & Catalog**: `ToolRegistry` exposes metadata (category `research|action|both`) and catalogs filtered by plan type and organization.
- **Validation**: The agent validates tool availability for the chosen plan type before execution.
- **Execution**: `ToolRunner` executes tools with `RetryPolicy` and `TimeoutPolicy`, emitting streaming progress/partial/error events. Results are persisted and included in SSE payloads.
- **Implementations**: Live under `tools/implementations/` with shared helpers in `tools/utils.py` and contracts in `tools/base.py`, `tools/metadata.py` and `tools/schemas/`.

### Context and builders (`context/`)
- `ContextHub` is the single entry point for building and refreshing context sections:
  - Static: schemas/files and other invariants.
  - Warm: messages/resources/observations updated each iteration.
  - Builders: instructions, messages, resources, observation history and summaries.
- The agent stores context snapshots (`initial`, `pre_tool`, `post_tool`, `final`) for auditing and analytics.

### Streaming & events
- SSE events: `block.upsert`, `decision.partial/final`, `planner.retry`, `tool.started/partial/progress/error/finished`, `query.created`, `visualization.created/updated`, `data_model.completed`, `block.delta.artifact`, `completion.finished`, and `instructions.suggest.*`.
- WebSocket broadcasting remains for backward compatibility alongside the SSE queue.

### Conventions
- Keep orchestration in `agent_v2.py`; do not embed business logic in tools.
- Tools must be deterministic and side-effect-aware; persist via services/managers, not directly in route handlers.
- Prefer typed Pydantic models for planner inputs/decisions/events and tool I/O schemas.

### Adding a feature (quick checklist)
1. Add/extend a tool under `tools/implementations/` and register it in `ToolRegistry`.
2. If needed, add metadata/schemas and utilities under `tools/`.
3. Update `ContextHub` builders if the planner requires new context.
4. Adjust `PromptBuilder` or planner schemas when the decision shape changes.
5. Orchestrate via `agent_v2.py` (validate tool category, stream events, persist artifacts).
6. Add tests under `backend/tests/ai` and e2e where tools impact user-visible flows.

