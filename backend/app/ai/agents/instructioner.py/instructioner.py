from app.ai.llm import LLM
from app.models.llm_model import LLMModel
from partialjson.json_parser import JSONParser

class Reporter:

    def __init__(self, model: LLMModel) -> None:
        self.llm = LLM(model)

    async def generate_report_title(self, schema, query_history=None, metadata_resources=None, data_source_id=None):

        text = f"""
        You are a data analyst tasked with generating instructions for a data source.

        Given the following schema:
        {schema}

        And the following query history:
        {query_history}

        And the following metadata resources:
        {metadata_resources}

        And the following data source id:
        {data_source_id}

        Generate 7 instructions for the data source.

        The instructions should be in the following format:
        - Avoid using Table A when Table B is more relevant
        - We calculate revenue by summing the total payments and multiplying by the exchange rate divided by 100
        - Active users are those who have logged in at least once in the last 30 days
        """

        return self.llm.inference(text)
