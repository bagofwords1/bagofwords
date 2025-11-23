from typing import Optional, Callable

from partialjson.json_parser import JSONParser
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import LLM
from app.models.llm_model import LLMModel

class Reporter:

    def __init__(
        self,
        model: LLMModel,
        usage_session_maker: Optional[Callable[[], AsyncSession]] = None,
    ) -> None:
        self.llm = LLM(model, usage_session_maker=usage_session_maker)

    async def generate_report_title(self, messages, plan):

        text = f"""
        You are a reporter tasked with generating a title for a report.

        Given the following messages
        {messages}

        And this plan:
        {plan}

        Generate a title for the report. Should be concise and descriptive of the report. Not more than 5 words.

        Your response should be just the title, nothing else. No quotes / markdown / etc.

        For example:
        "Generate a report with a bar chart of the top 5 countries by population" -> Top 5 Countries by Population
        "Generate a report with a line chart of the stock price of Tesla" -> Tesla Stock Price
        "Generate a report with a scatter plot of the relationship between age and income" -> Age vs Income
        "Generate a report with a heatmap of the correlation between different stocks" -> Stock Correlation
        "Generate a list of customers who have bought the most from us" -> Top Customers
        "Reconcile inventory between our system and our warehouse" -> Inventory Reconciliation
        """

        return self.llm.inference(text)
