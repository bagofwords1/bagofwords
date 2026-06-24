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

    async def generate_follow_ups(self, messages_context, max_suggestions: int = 5):
        """Suggest a few natural follow-up questions for the user to ask next.

        Runs on the small/default model (the LLM this Reporter was constructed
        with). Returns a list of short question strings (never raises — returns
        [] on any parsing/LLM error so a failure can never break the run).
        """
        text = f"""
        You are helping a user explore their data. Given the recent conversation
        with an analytics assistant, propose up to {max_suggestions} natural
        follow-up questions the user might ask next.

        Conversation so far:
        {messages_context}

        Rules:
        - Each suggestion is a single, self-contained question the user could click to ask next.
        - Keep them short (max ~12 words), specific, and genuinely useful given the conversation.
        - Do not repeat questions already asked. Do not number them.
        {build_language_directive(self.organization_settings)}
        Return ONLY a JSON array of strings, nothing else.
        Example: ["How did revenue trend last quarter?", "Which region grew fastest?"]
        """

        try:
            raw = await asyncio.to_thread(
                self.llm.inference, text, usage_scope="report.follow_ups"
            )
        except Exception:
            return []

        return self._parse_follow_ups(raw, max_suggestions)

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
