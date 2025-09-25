from app.project_manager import ProjectManager
import os
from app.models.data_source import DataSource
from app.ai.llm import LLM
import pandas as pd
import json
from app.models.llm_model import LLMModel

"""
"""

class DataSourceAgent:

    def __init__(self, data_source: DataSource, schema: str, model: LLMModel):
        self.data_source = data_source
        self.llm = LLM(model)
        self.schema = schema

    def generate_summary(self):
        prompt = f"""
Given this data source:
{self.data_source.name}

And this schema
{self.schema}

Please describe the data source in a few sentences. Make it useful for a non-technical audience. 

Guidelines:
- High level overview of the data source
- What the data source is for
- How it can be used
- Key tables, fields and relationships
- Explicit and implicit relationships
- Ignore metadata tables and fields. or system tables and fields.
- Use simple language, make it extremely to point, not fluff, and be very brief and concise.

Respond only markdown text (with newlines), no json or any other formatting.
"""
        response = self.llm.inference(prompt)
        return response

    def generate_conversation_starters(self):
        prompt = f"""
Given this data source:
{self.data_source.name}

And this schema
{self.schema}

Please generate 4 conversation starters. Return them in a strict JSON array format.

The response should be an array of strings, where each string contains a title and detailed prompt separated by a newline character.

A few examples:
- Title: What are the top 10 customers by revenue?
  Prompt: List the top 10 customers by revenue. Measure revenue by summing the total payments. Show name, email,  geo, total revenue, and total payments
- Title: What are the best selling products?
  Prompt: List the top 10 products by revenue. Measure revenue by summing the total payments. Show name, total revenue, and total payments
- Title: Who are the most unhappy customers?
  Prompt: List the top 10 customers by negative reviews. Show name, email, geo, total reviews, and total negative reviews

Example format:
[
    "Starter 1 Title\\nStarter 1 detailed prompt",
    "Starter 2 Title\\nStarter 2 detailed prompt",
    "Starter 3 Title\\nStarter 3 detailed prompt",
    "Starter 4 Title\\nStarter 4 detailed prompt"
]

Important: in the output, do not include "Title" or "Prompt" in the output. just the list of conversation starters. Separete between Title and Prompt with a newline character.
Important: Return only the JSON array with no additional text, formatting, or explanations. Ensure all newlines are properly escaped with \\n.
Do not add prefix ``` or markdown or anything. just the list of conversation starters.
"""

        response = self.llm.inference(prompt)
        # Strip any potential whitespace or extra characters
        response = response.strip()
        json_response = json.loads(response)
        list_response = list(json_response)

        return list_response
    

    def generate_context(self):
        pass


    def generate_description(self):
        prompt = f"""
Given this data source:
{self.data_source.name}

And this schema
{self.schema}

Please review the schema and the data source name and it client. Then, understand the nature of the data source, think about the purpose of the data source, and generate a description for the data source. Make it useful for a non-technical audience. 
Description should be max 3 sentences. Should be concise, valuable, and useful.

Guidelines:
- Make it personalized based on the data schema. 
- Don't make it generic.
- Don't make it too long. Max 3 sentences. No more than 280 characters.
- Don't use fluff words. Be direct and to the point.
- Be factual when describing the data source.
- Use simple language, make it extremely to point, not fluff, and be very brief and concise.
- Don't say "This data source contains information about...". Just describe the data source.

Examples:
- "Salesforce CRM data that provides information about customers, opportunities, leads, and marketing campaigns."
- "Google Analytics data that provides information about website traffic, user behavior, and marketing effectiveness."
- "Jira data that provides information about engineering projects, tasks, and team performance."
"""
        response = self.llm.inference(prompt)
        return response