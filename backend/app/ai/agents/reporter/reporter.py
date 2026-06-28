from typing import Optional, Callable

import asyncio

from partialjson.json_parser import JSONParser
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import LLM
from app.ai.prompt_language import build_language_directive
from app.models.llm_model import LLMModel
from app.schemas.organization_settings_schema import OrganizationSettingsConfig
from app.services.usage_policy_service import UsageLimitContext

class Reporter:

    def __init__(
        self,
        model: LLMModel,
        organization_settings: Optional[OrganizationSettingsConfig] = None,
        usage_session_maker: Optional[Callable[[], AsyncSession]] = None,
        usage_context: Optional[UsageLimitContext] = None,
    ) -> None:
        self.llm = LLM(model, usage_session_maker=usage_session_maker, usage_context=usage_context)
        self.organization_settings = organization_settings

    async def generate_report_title(self, messages, plan):

        text = f"""
        You are a reporter tasked with generating a title for a report.

        Given the following messages
        {messages}

        And this plan:
        {plan}

        Generate a title for the report. Should be concise and descriptive of the report. Not more than 5 words.
        {build_language_directive(self.organization_settings)}
        Your response should be just the title, nothing else. No quotes / markdown / etc.

        For example:
        "Generate a report with a bar chart of the top 5 countries by population" -> Top 5 Countries by Population
        "Generate a report with a line chart of the stock price of Tesla" -> Tesla Stock Price
        "Generate a report with a scatter plot of the relationship between age and income" -> Age vs Income
        "Generate a report with a heatmap of the correlation between different stocks" -> Stock Correlation
        "Generate a list of customers who have bought the most from us" -> Top Customers
        "Reconcile inventory between our system and our warehouse" -> Inventory Reconciliation
        """

        # `LLM.inference` is sync and runs the pre-call quota check via
        # `run_blocking`. Called from a running event loop with no `loop`
        # wired on the usage context, that check raises immediately. Offload
        # to a worker thread so the sync check has no loop to collide with.
        return await asyncio.to_thread(
            self.llm.inference, text, usage_scope="report.title"
        )

    async def generate_follow_ups(
        self,
        messages_context,
        *,
        mode: str = "chat",
        schemas_context: str = "",
        instructions_context: str = "",
        max_suggestions: int = 5,
    ):
        """Suggest a few follow-up prompts for the user to click next.

        The prompt is tailored to the agent mode:
          - ``training``: suggestions are next *training* actions (review weak
            runs, find instruction gaps, draft/refine instructions) — no data
            schema involved, matching how training mode operates.
          - ``chat`` / ``deep`` (default): data-exploration questions, grounded
            in the available schema + data-source descriptions + instructions so
            suggestions reference dimensions/metrics that actually exist.

        Runs on the small/default model. Never raises — returns [] on any
        parsing/LLM error so a failure can't break the run.
        """
        if mode == "training":
            text = self._training_follow_ups_prompt(
                messages_context, schemas_context, instructions_context, max_suggestions
            )
        else:
            text = self._chat_follow_ups_prompt(
                messages_context, schemas_context, instructions_context, max_suggestions
            )

        try:
            raw = await asyncio.to_thread(
                self.llm.inference, text, usage_scope="report.follow_ups"
            )
        except Exception:
            return []

        return self._parse_follow_ups(raw, max_suggestions)

    def _chat_follow_ups_prompt(self, messages_context, schemas_context, instructions_context, max_suggestions):
        data_blocks = ""
        if schemas_context:
            data_blocks += f"\n        Available data (tables, columns, data-source descriptions):\n        {schemas_context}\n"
        if instructions_context:
            data_blocks += f"\n        Business context / instructions:\n        {instructions_context}\n"

        grounding_rule = (
            "- When a suggestion IS a data question, ground it in the available data above — reference "
            "dimensions, metrics, segments, or time columns that actually exist, and never invent metrics the data can't support."
            if schemas_context else
            "- When a suggestion is a data question, keep it answerable from the kind of data discussed; do not invent specifics."
        )

        return f"""
        You are suggesting what a user might click to ask next in an assistant conversation.
        The RECENT CONVERSATION is the primary driver: every suggestion must be a natural
        continuation of what the user and assistant were just doing. Propose up to
        {max_suggestions} follow-ups.

        Conversation so far:
        {messages_context}
        {data_blocks}
        First, read the conversation to decide what kind of follow-ups fit:
        - If the last turn was a data/analytics question, suggest natural next data questions,
          grounded in the available data above.
        - If the last turn was NOT a data question (e.g. scheduling a task, sending an
          email/notification, changing a setting, or another non-analytical request), suggest
          follow-ups that continue THAT task. Do not pivot to unrelated data questions just
          because data happens to be available — the available data is supporting context only.

        Rules:
        - Each suggestion is a single, self-contained prompt the user could click to send next.
        - Keep them short (max ~12 words), specific, and genuinely useful given the conversation.
        - Suggestions must follow from the recent conversation — never generic questions disconnected from it.
        {grounding_rule}
        - Do not repeat questions or actions already done. Do not number them.
        - Write the suggestions in the SAME language the conversation above is in. Keep column names, identifiers, and metric names as-is.
        Return ONLY a JSON array of strings, nothing else.
        Example (data turn): ["How did revenue trend last quarter?", "Which region grew fastest?"]
        Example (non-data turn, e.g. a scheduled email): ["Change the daily send time?", "Stop the daily email", "Also send it to my manager?"]
        """

    def _training_follow_ups_prompt(self, messages_context, schemas_context, instructions_context, max_suggestions):
        context_blocks = ""
        if instructions_context:
            context_blocks += f"\n        Current agent instructions:\n        {instructions_context}\n"
        if schemas_context:
            context_blocks += f"\n        Available data (tables, columns, data-source descriptions):\n        {schemas_context}\n"

        return f"""
        You are helping an admin improve this AI analytics system in TRAINING MODE.
        In training mode the admin reviews the agent's performance and curates the
        instruction set that steers it — they are NOT exploring business data.
        {context_blocks}
        Given the recent conversation and the context above, propose up to
        {max_suggestions} follow-up actions the admin might take next, each phrased
        as a short clickable prompt. Good training follow-ups do things like:
        - Audit the instruction set for problems: conflicting rules, overlapping or
          redundant instructions, or duplicates that should be merged.
        - Surface coverage gaps — schema areas, metrics, or business terms with NO
          instruction, or definitions that are ambiguous.
        - Review past agent runs that need attention (low-confidence answers, failed
          queries, negative feedback, low instruction coverage).
        - Create, merge, refine, or remove a specific instruction based on the above.

        Reference concrete instruction topics / table / metric names from the context
        when you can, so each action is specific and clickable.

        Conversation so far:
        {messages_context}

        Rules:
        - Each suggestion is a single, self-contained training action phrased as a prompt.
        - Keep them short (max ~12 words), specific, and actionable.
        - Do not repeat actions already taken. Do not number them.
        - Write the suggestions in the SAME language the conversation above is in. Keep instruction text, table names, and identifiers as-is.
        Return ONLY a JSON array of strings, nothing else.
        Example: ["Find conflicting instructions about revenue", "Which tables have no instructions?"]
        """

    @staticmethod
    def _parse_follow_ups(raw, max_suggestions: int = 5):
        """Best-effort parse of the model output into a clean list of strings."""
        import json
        import re

        if not raw or not isinstance(raw, str):
            return []

        text = raw.strip()
        # Strip ```json ... ``` fences if the model added them.
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text).strip()

        items = None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                items = parsed
        except Exception:
            # Fall back to the partial JSON parser used elsewhere in the codebase.
            try:
                parsed = JSONParser().parse(text)
                if isinstance(parsed, list):
                    items = parsed
            except Exception:
                items = None

        if items is None:
            return []

        cleaned = []
        seen = set()
        for it in items:
            if not isinstance(it, str):
                continue
            q = it.strip().strip('"').strip()
            if not q:
                continue
            key = q.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(q)
            if len(cleaned) >= max_suggestions:
                break
        return cleaned
