from app.ai.llm import LLM
from app.models.llm_model import LLMModel
from app.schemas.organization_settings_schema import OrganizationSettingsConfig
from app.ai.context.instruction_context_builder import InstructionContextBuilder
class Answer:

    def __init__(self, model: LLMModel, organization_settings: OrganizationSettingsConfig, instruction_context_builder: InstructionContextBuilder) -> None:
        self.llm = LLM(model)
        self.organization_settings = organization_settings
        self.code_reviewer = organization_settings.get_config("code_reviewer").value
        self.search_context = organization_settings.get_config("search_context").value
        self.allow_llm_see_data = organization_settings.get_config("allow_llm_see_data").value
        self.instruction_context_builder = instruction_context_builder

    async def execute(self, prompt, schemas, memories, previous_messages, widget=None, observation_data=None, external_platform=None):
        # --------------------------------------------------------------
        # NEW – fetch instruction context
        # --------------------------------------------------------------
        instructions_context = await self.instruction_context_builder.get_instructions_context()
        
        # Build observation context similar to planner
        
        observation_context = ""
        if observation_data and "widgets" in observation_data and observation_data["widgets"]:
            widget_summaries = []
            
            for widget_data in observation_data["widgets"]:
                widget_title = widget_data.get('widget_title', 'N/A')
                widget_type = widget_data.get('widget_type', 'unknown')
                row_count = widget_data.get('row_count', 'unknown')
                column_names = widget_data.get('column_names', [])
                # Add data section
                data_section = ""
                if "data" in widget_data:
                    data_rows = widget_data["data"]
                    if data_rows:
                        data_section = "\n**Full Data**:\n"
                        # Create table header
                        data_section += " | ".join(column_names) + "\n"
                        data_section += "-" * (sum(len(col) for col in column_names) + 3 * len(column_names)) + "\n"
                        # Add all rows
                        for row in data_rows:
                            data_section += " | ".join(str(row.get(col, "N/A")) for col in column_names) + "\n"
                
                stats_summary = "No statistics available"
                if widget_data.get('stats'):
                    stats = widget_data.get('stats')
                    if isinstance(stats, dict):
                        column_info = stats.get('column_info', {})
                        stats_summary = "**Column Statistics**:\n"
                        for col, info in column_info.items():
                            stats_summary += f"\n{col}:\n"
                            for stat, value in info.items():
                                stats_summary += f"- {stat}: {value}\n"
                
                widget_summary = f"""
                **Widget: {widget_title}**
                Type: {widget_type}
                Total Rows: {row_count}
                Columns: {', '.join(column_names)}
                
{data_section if self.allow_llm_see_data else "No data preview is allowed in settings"}                

                {stats_summary}
                """
                widget_summaries.append(widget_summary)
            
            observation_context = "\n\n".join([
                "**Available Widgets and Their Data:**",
                *widget_summaries
            ])

        text = f"""
You are a data analyst. Your general capabilities are:
- creating data tables from any data source: databses, APIs, files, etc
- creating charts and dashboards
- cleaning, analyzing, and transforming data

Metadata about the user:
- The user sent a message via {external_platform} platform. Make sure to format the output and style based on the platform -- use `mrkdwn` formatting ONLY, no HTML.

The planner agent decided that you should answer the question below with the data and schemas provided.

Instructions:
**VERY IMPORTANT, CREATED BY THE USER, MUST BE USED AND CONSIDERED**:
{instructions_context}

You have been given:

- Schemas:
{schemas}

- Selected Widget:
{widget.title if widget else "No widget available"}

- Memories:
{memories}

- Previous messages:
{previous_messages}

{observation_context}

- User Question:
{prompt}

**Guidelines:**

0. You can summarize, explain, or answer the question in a concise manner.
1. Be kind, friendly and helpful.
2. Your answer should be based solely on the given schemas and widgets data.
3. If the question cannot be answered using the the context, respond nicely and ask for more information/clarification.
4. Answer briefly and directly without repeating the question or referencing the context.
5. Do not mention the widget sample data, schemas, previous messages, or your reasoning process—just answer the user's question.
6. { "Do not" if self.code_reviewer else "Do" } provide code, SQL, or technical implementation details unless specifically asked. Focus on a human-friendly, straightforward explanation.
7. If the user asks about relationships between tables, give a brief, human-readable explanation (e.g., "invoice table (payment_id) and payment table (id)").
8. If asked about a table's schema, provide a concise and human-readable summary (e.g., "invoice table has columns: id, amount, date, customer_id").

**Data Summarization and Analysis:**

1. Do not create summaries over made up data -- use only the data provided in the context and widgets.
2. Make the analysis brief and clear.
3. If the data is not relevant or not enough, just say so.
4. Do not repeat the dataframe in your answer. The user already sees the dataframe in the widget.


Output Format:
1. You may use simple HTML and Markdown for formatting. For emphasis, you can use:
   - **<b>bold</b>**
   - <i>italic</i>
   - <u>underline</u>
   - <ul>unordered lists</ul> with <li>items</li>
   - <ol>ordered lists</ol>
   - <span class="text-red-500">Tailwind classes</span> for styling
   - Tables using <table>, <tr>, <th>, <td>
2. No JSON output. Just return the formatted text as your answer.

Now, provide your answer following these guidelines.
"""
        
        chunk_buffer = ""
        chunk_count = 0
        
        async for chunk in self.llm.inference_stream(prompt=text):
            chunk_buffer += chunk
            chunk_count += 1
            
            if chunk_count == 5:
                yield chunk_buffer
                chunk_buffer = ""
                chunk_count = 0
        
        # Yield any remaining chunks if they exist
        if chunk_buffer:
            yield chunk_buffer
    