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
Data source name: {self.data_source.name}

Schema:
{self.schema}

Write exactly 2 sentences describing this data source. First sentence: what it is. Second sentence: what key data it contains.

Rules:
- No fluff, no "this data source contains", no markdown, no bullet points.
- Plain text only.
- Max 60 words total.
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
- Title: Top Customers
  Prompt: List the top 10 customers by revenue. Measure revenue by summing the total payments. Show name, email,  geo, total revenue, and total payments
- Title: Best Sellers
  Prompt: List the top 10 products by revenue. Measure revenue by summing the total payments. Show name, total revenue, and total payments
- Title: Unhappy Customers
  Prompt: List the top 10 customers by negative reviews. Show name, email, geo, total reviews, and total negative reviews
- Title: Customers Churn Root Cause
  Prompt: List the top 10 customers by churn. Show name, email, geo, total churn, and total churn reason.
- Title: Stray Cloud Users
  Prompt: List the top 10 users who are not in the cloud. Show name, email, geo, total users, and total users not in the cloud.

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

    def generate_datasource_instruction(self) -> dict:
        """Generate a single comprehensive overview instruction for this data source.

        Returns a dict with keys: title, text, category, load_mode, confidence.
        Title follows the pattern {DS_NAME}_OVERVIEW.
        """
        ds_name = self.data_source.name or "datasource"
        title = ds_name.upper().replace(" ", "_").replace("-", "_") + "_OVERVIEW"

        prompt = f"""You are a data analyst onboarding a new data source for an AI analytics agent.

Data source/Agent name: {ds_name}

Schema:
{self.schema}

Your task: write a single comprehensive instruction that the AI analytics agent will load every time it works with this data source. This instruction is its permanent reference card.

The instruction must cover:
- What this data source represents and its business purpose
- Key tables and what each tracks (1 line per table, focused on business meaning)
- Important relationships between tables (joins, foreign keys worth knowing)
- Any naming conventions, column quirks, or non-obvious facts visible in the schema (e.g. status codes, units, soft deletes)
- Primary identifiers and date/time columns to use for filtering

Rules:
- Be specific and factual — ground every claim in the schema, never invent
- No fluff, no generic statements like "this data source contains data about..."
- Write as direct instructions to the AI analyst ("Use X to...", "Always join on...", "Note that...")
- Plain prose with short bullet points where helpful
- 150–300 words

Return JSON only:
{{"title": "{title}", "text": "...", "category": "general", "load_mode": "always", "confidence": 0.95}}"""

        response = self.llm.inference(prompt)
        response = response.strip()
        # Strip markdown code fences if present
        if response.startswith("```"):
            response = response.split("```")[1]
            if response.startswith("json"):
                response = response[4:]
        try:
            return json.loads(response.strip())
        except Exception:
            return {
                "title": title,
                "text": response.strip(),
                "category": "general",
                "load_mode": "always",
                "confidence": 0.9,
            }


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