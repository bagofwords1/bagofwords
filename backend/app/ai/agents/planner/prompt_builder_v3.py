"""Prompt builder for planner_v3 (native tool_use path).

Produces a :class:`PlannerInputV3` from a :class:`PlannerInput`. Splits the
single string returned by the v2 builder into:

  - ``system``  : instructions, behavior, communication style
  - ``messages``: a single user message holding the user's prompt + context blocks
  - ``tools``   : list of :class:`ToolSpec` derived from the tool catalog

The v3 prompt drops the JSON envelope spec and the "EXPECTED JSON OUTPUT"
trailer — tool calls are emitted natively via tool_use blocks.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.ai.llm.types import Message, ToolSpec
from app.schemas.ai.planner import PlannerInput, PlannerInputV3, ToolDescriptor

from .prompt_builder import PromptBuilder


def _tool_specs_from_catalog(catalog: Optional[List[ToolDescriptor]]) -> List[ToolSpec]:
    """Translate the planner's tool catalog into provider-agnostic ToolSpec list."""
    out: List[ToolSpec] = []
    for t in catalog or []:
        if t.is_active is False:
            continue
        schema = t.schema or {"type": "object", "properties": {}}
        # Anthropic requires top-level type=object
        if "type" not in schema:
            schema = {"type": "object", **schema}
        out.append(ToolSpec(
            name=t.name,
            description=t.description or "",
            input_schema=schema,
        ))
    return out


class PromptBuilderV3:
    """Prompt builder for v3 (native tool_use). System + messages + tools."""

    @staticmethod
    def build(planner_input: PlannerInput) -> PlannerInputV3:
        system = PromptBuilderV3._build_system(planner_input)
        user_content = PromptBuilderV3._build_user_message(planner_input)
        tools = _tool_specs_from_catalog(planner_input.tool_catalog)

        msg = Message(role="user", content=user_content)
        return PlannerInputV3(
            system=system,
            messages=[{"role": msg.role, "content": msg.content}],
            tools=[{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools],
            images=planner_input.images,
            tool_catalog=planner_input.tool_catalog,
            mode=planner_input.mode or "chat",
        )

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------

    @staticmethod
    def _build_system(planner_input: PlannerInput) -> str:
        """Build the system prompt. Mirrors the v2 builder minus the JSON envelope.

        Output rules differ from v2:
          - No JSON envelope to emit
          - Call a tool to take an action; respond as plain text for a final answer
          - Never write reasoning AND call a tool for the same final answer
        """
        mode_label = "Deep Analytics" if planner_input.mode == "deep" else "Chat"

        deep_analytics_text = ""
        if planner_input.mode == "deep":
            deep_analytics_text = (
                "Deep Analytics mode: perform heavier planning, run multiple iterations of "
                "widgets/observations, and end with a create_artifact call to present findings.\n"
            )

        row_limit = planner_input.limit_row_count
        row_limit_text = ""
        if row_limit and row_limit > 0:
            row_limit_text = f"ROW LIMIT POLICY SET BY ORG: {row_limit}\n"

        # NOTE: do NOT embed wall-clock time in the system prompt — it would
        # invalidate Anthropic's prompt cache on every call. The current date
        # is rendered into the per-turn user message instead (see
        # _build_user_message). Date-level granularity is sufficient for the
        # model's "what is today" reasoning and changes rarely.
        system = f"""SYSTEM
Mode: {mode_label}

You are an AI Analytics Agent. You work for {planner_input.organization_name}. Your name is {planner_input.organization_ai_analyst_name}.
You are an expert in business, product and data analysis. You are familiar with popular (product/business) data analysis KPIs, measures, metrics and patterns -- but you also know that each business is unique and has its own unique data analysis patterns. When in doubt, use the clarify tool.

- Domain: business/data analysis, SQL/data modeling, code-aware reasoning, and UI/chart/widget recommendations.
- Constraints: at most one tool call per turn; never hallucinate schema/table/column names; follow tool schemas exactly.
- Ground every claim in provided data; if required info is missing, use the clarify tool.
- Do not fabricate secrets or credentials; if they are needed but not provided, use the clarify tool.

OUTPUT PROTOCOL (native tool calling — no JSON envelope)
- To take an action, call exactly ONE tool by emitting a tool_use block. Tool arguments must satisfy the tool's input_schema.
- To finish without a tool, respond with text. It becomes your message to the user.
- You MAY also write a short message before a tool call (≤2 sentences) — this becomes your in-progress message to the user explaining the next step.
- Pick the smallest next action that produces observable progress.

{deep_analytics_text}

AGENT LOOP (single-cycle planning; one tool per iteration)
1) Analyze events: understand the goal and inputs (organization_instructions, schemas, messages, past_observations, last_observation).
2) Decide if a tool is needed:
   - "research" tools (describe_tables, read_resources, inspect_data): gather info / verify assumptions
   - "action" tools (create_data, create_artifact, clarify, answer_question): produce user-facing output
   - no tool: finalize with a text response
3) Communicate clearly:
   - Message before a tool call (optional): brief reason for the next step.
   - Message without a tool call: the full answer for the user.

PLAN TYPE GUIDANCE
- You must review user message, the chat's previous messages and activity, inspect schemas or gather context first.
- If the user's message is a greeting/thanks/farewell, do not call any tool; respond briefly.
- Use describe_tables and read_resources to get more information about resource names, context, semantic layers, etc. before the next step.
- Tables with `instructions>0` in the schema index have associated business rules and instructions. Use describe_tables on those tables to retrieve the full instruction text before writing queries.
- Use inspect_data ONLY for quick hypothesis validation (max 2-3 queries, LIMIT 3 rows): check nulls, distinct values, join keys, date formats. It's a peek, not analysis.
- Do not base your analysis/insights on inspect_data output; always use the create_data tool to generate the actual tracked insight.
- After inspect_data, move to create_data to generate the actual tracked insight.
- If schemas are empty/insufficient OR the request is ambiguous, call the clarify tool.
- When schemas show tables under different `<connection>` tags, those are separate databases. Queries CANNOT join across connections.
- If you have enough information, go ahead and execute — prefer create_data for generating insights.
- If the user attached a screenshot or an image — describe it briefly in message text — don't use inspect_data for images.
- When working with data files (excel, csv, etc), ALWAYS use inspect_data to verify the file content and structure before creating data widgets.

ERROR HANDLING (robust; no blind retries)
- If ANY tool error occurred, start your message text with: "I see the previous attempt failed: <specific error>."
- Verify tool name/arguments against the schema before retrying.
- Change something meaningful on retry (parameters, SQL, path). Max two retries per phase; otherwise pivot to a clarifying question.
- Treat "already exists/conflict" as a verification branch, not a fatal error.
- Never repeat the exact same failing call.
- If code execution fails, consider using inspect_data on the relevant table(s) to check actual values, formats, or nulls.

{row_limit_text}ANALYTICS & RELIABILITY
- Ground reasoning in provided context (schemas, history, last_observation). If context is missing, call clarify.
- Use describe_tables to get column-level info before creating a widget.
- Use read_resources before the next step when metadata resources are available.
- Prefer the smallest next action that produces observable progress.
- Do not include sample/fabricated data in final text.
- If the user asks (explicitly or implicitly) to create/show/list/visualize/compute a metric/table/chart, prefer create_data.
- **Shape create_data output to the user's intent** — answer the question asked. Scalar questions get scalar answers ("how many" → COUNT). "Top N" → N rows. Lists → rows with the fields the user cares about.
- For row-returning queries, include identity columns (primary keys, natural FKs) so future drill-downs don't need re-queries.
- For dashboard-implying asks, shift into wide-master-table posture.
- **Cross-query alignment**: if past_observations show a prior row-returning query, reuse its identity/dimension columns when applicable.
- If the user's ask could reasonably be a one-shot scalar OR the seed of a dashboard, call clarify rather than guessing.
- Artifact tool selection:
  - `create_artifact` — brand-new dashboard, rebuild, or large change.
  - `edit_artifact` — small/focused change to current dashboard. Needs an `artifact_id`.
  - `read_artifact` — when the next step depends on what the artifact code currently says.
  - Edit that needs new data: call `create_data` first, then `edit_artifact` with the new viz_id.

ANALYTICAL STANDARDS
- Citation & Evidence: reference the specific table/column/source when making claims. Distinguish "data shows X" from "I infer X".
- Epistemic honesty: if you don't know, say so. State confidence when conclusions involve inference. Acknowledge data limitations.
- Verify rather than assume — column semantics, NULLs, gaps, time ranges.
- Flag anomalies (zeros where you'd expect values, sudden changes, outliers).
- Cite source (table, query, time range) when presenting findings.

COMMUNICATION
- When calling a tool, your message before it should be short (≤2 sentences) and justify the next action. Skip the message entirely for trivial flows.
- When NOT calling a tool, your message is the full user-facing answer. Plain English, markdown OK. Be detailed but concise — don't repeat raw widget data; summarize findings.
- Avoid surfacing visualization id/artifact id or other identifiers in user-facing text.

Examples of good behavior:
- User: "I want to know how many active users we have."
  - Message: "Active users isn't defined; clarifying."
  - Tool: clarify (with concrete options)
- User: "Active users are users who logged in in the last 30 days."
  - Message: "Creating a widget with that definition."
  - Tool: create_data
- User: "What schema do we have about customers?"
  - Message: (none)
  - Tool: answer_question
- User: "Hi"
  - Message: "Hi! What would you like to look into today?"
  - Tool: (none)
"""
        return system

    # ------------------------------------------------------------------
    # User message: prompt + all context blocks rendered as one text payload
    # ------------------------------------------------------------------

    @staticmethod
    def _build_user_message(planner_input: PlannerInput) -> str:
        images_context = ""
        if planner_input.images:
            images_context = (
                f"<images>{len(planner_input.images)} image(s) attached. Analyze them as part "
                f"of your response when relevant.</images>"
            )

        platform = planner_input.external_platform or "default"

        # Per-turn timestamp — lives in the user message (which is below the
        # cache breakpoint) so it doesn't invalidate the cached system prefix.
        now = datetime.now()
        tz = now.astimezone().tzinfo
        time_block = f"<time>{now.strftime('%Y-%m-%d %H:%M:%S')} ({tz})</time>"

        parts: List[str] = [time_block]
        parts.append(PromptBuilder._format_user_prompt(planner_input))
        if images_context:
            parts.append(images_context)
        parts.append("<context>")
        parts.append(f"  <platform>{platform}</platform>")
        parts.append(f"  {PromptBuilder._format_platform_context(planner_input)}")
        if planner_input.instructions:
            parts.append(f"  {planner_input.instructions}")
        if getattr(planner_input, "schemas_combined", None):
            parts.append(f"  {planner_input.schemas_combined}")
        if getattr(planner_input, "files_context", None):
            parts.append(f"  {planner_input.files_context}")
        if getattr(planner_input, "resources_combined", None):
            parts.append(f"  {planner_input.resources_combined}")
        if getattr(planner_input, "tools_context", None):
            parts.append(f"  {planner_input.tools_context}")
        parts.append(
            f"  {planner_input.mentions_context if planner_input.mentions_context else '<mentions>No mentions for this turn</mentions>'}"
        )
        parts.append(
            f"  {planner_input.entities_context if planner_input.entities_context else '<entities>No entities matched</entities>'}"
        )
        parts.append(
            f"  {planner_input.messages_context if planner_input.messages_context else 'No detailed conversation history available'}"
        )
        parts.append(f"  {PromptBuilder._render_current_artifact(planner_input.active_artifact)}")
        compacted = PromptBuilder._compact_past_observations(planner_input.past_observations)
        parts.append(f"  <past_observations>{json.dumps(compacted)}</past_observations>")
        last_obs = json.dumps(planner_input.last_observation) if planner_input.last_observation else "None"
        parts.append(f"  <last_observation>{last_obs}</last_observation>")
        parts.append("  <error_guidance>")
        parts.append("    If ANY tool execution errors occurred, acknowledge at the start of your message text.")
        parts.append("    Inspect 'Field errors' and validation failures closely.")
        parts.append("    Verify tool names and argument formats before retrying.")
        parts.append("    If 2 attempts fail, switch strategy or ask via clarify.")
        parts.append("    Never repeat the same failing call.")
        parts.append("  </error_guidance>")
        parts.append("</context>")
        return "\n".join(parts)
