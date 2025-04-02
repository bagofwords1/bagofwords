from partialjson.json_parser import JSONParser
from app.ai.llm import LLM
from app.models.llm_model import LLMModel
import tiktoken  # Add this import for token counting
import json

class Planner:

    def __init__(self, model: LLMModel, organization_settings: dict) -> None:
        self.llm = LLM(model)
        self.organization_settings = organization_settings

        # Handle tokenizer selection with better fallback logic
        try:
            if hasattr(model, 'name'):
                # Try to get encoding for the model
                self.tokenizer = tiktoken.encoding_for_model(model.name)
            else:
                # Fallback to cl100k_base
                self.tokenizer = tiktoken.get_encoding("cl100k_base")
        except KeyError:
            # If model name isn't recognized (like GPT-4o), use cl100k_base
            print(f"Warning: Could not find tokenizer for {model.name if hasattr(model, 'name') else 'unknown model'}. Using cl100k_base instead.")
            self.tokenizer = tiktoken.get_encoding("cl100k_base")

    def count_tokens(self, text):
        """Count the number of tokens in a text string."""
        if not text:
            return 0
        return len(self.tokenizer.encode(text))

    async def execute(self, schemas, persona, prompt, memories, previous_messages, observation_data=None, widget=None, step=None):
        # Generate observation context if observation_data is provided
        observation_context = ""
        if observation_data and "widgets" in observation_data and observation_data["widgets"]:
            # Create summaries for all widgets
            widget_summaries = []
            
            for widget_data in observation_data["widgets"]:
                # Extract metadata
                widget_title = widget_data.get('widget_title', 'N/A')
                widget_type = widget_data.get('widget_type', 'unknown')
                row_count = widget_data.get('row_count', 'unknown')
                column_names = widget_data.get('column_names', [])
                
                # Build data preview section
                data_preview_section = ""
                if "data_preview" in widget_data:
                    data_preview_section = f"""
                    **Data Preview**:
                    {widget_data['data_preview']}
                    """
                else:
                    data_preview_section = """
                    **Data Preview**:
                    Data preview is not available
                    """
                
                # Build statistics summary
                stats_summary = "No statistics available"
                if widget_data.get('stats'):
                    stats = widget_data.get('stats')
                    if isinstance(stats, dict):
                        # Format stats into a more readable form
                        stats_summary = f"""
                        Total Rows: {stats.get('total_rows', 'N/A')}
                        Total Columns: {stats.get('total_columns', 'N/A')}
                        Column Details: {', '.join(stats.get('column_names', []))}
                        """
                
                # Create summary for this widget
                widget_summary = f"""
                **Widget: {widget_title}**
                Widget Type: {widget_type}
                Total Rows: {row_count}
                Columns: {', '.join(column_names)}
                
                {data_preview_section}
                
                **Data Statistics**:
                {stats_summary}
                """
                widget_summaries.append(widget_summary)
            
            # Combine all widget summaries
            all_widgets_summary = "\n\n".join(widget_summaries)
            
            # Build the complete observation context
            observation_context = f"""
            **ANALYSIS RESULTS FROM ALL WIDGETS**:
            
            {all_widgets_summary}
            
            **Observation Instructions**:
            Based on ALL the widget results above:
            1. FIRST, check if the widgets match what the user originally requested
            2. If ALL of the requested widgets have been created successfully, you MUST set "analysis_complete" to TRUE
            3. Only set "analysis_complete" to FALSE if some part of the original request is missing/lacking and still unaddressed
            4. If analysis_complete is FALSE, provide an explanation of what is missing/lacking and still unaddressed in the reasoning section

            DIRECT INSTRUCTION: When the user's request is fully satisfied by the existing widgets, 
            set "analysis_complete" to TRUE and do NOT include any create_widget actions in your plan.
            """
        elif observation_data:
            # Fallback for legacy format (single widget)
            # Extract metadata
            widget_title = observation_data.get('widget_title', 'N/A')
            widget_type = observation_data.get('widget_type', 'unknown')
            row_count = observation_data.get('row_count', 'unknown')
            column_names = observation_data.get('column_names', [])
            
            # Build data preview section
            data_preview_section = ""
            if "data_preview" in observation_data:
                data_preview_section = f"""
                **Data Preview**:
                {observation_data['data_preview']}
                """
            else:
                data_preview_section = """
                **Data Preview**:
                Data preview is not available
                """
            
            # Build statistics summary
            stats_summary = "No statistics available"
            if observation_data.get('stats'):
                stats = observation_data.get('stats')
                if isinstance(stats, dict):
                    stats_summary = "Statistics available in info section"
            
            observation_context = f"""
            **Widget Information**:
            Widget: {widget_title}
            Widget Type: {widget_type}
            Total Rows: {row_count}
            Columns: {', '.join(column_names)}
            
            {data_preview_section}
            
            **Data Statistics**:
            {stats_summary}
            
            **Observation Instructions**:
            Based on the information above, determine if the analysis is complete...
            """

        design_dashboard_example = """
        Example 4 (design_dashboard):
        {{
            "analysis_complete": true,
            "reasoning": "I've analyzed all the widgets and found no further analysis needed. Setting analysis_complete to true.",
            "plan": [
                {{
                    "action": "design_dashboard",
                    "prefix": "Finally, let's combine all insights into a dashboard. I will place the bar chart of revenue by month and the line chart of revenue by year in the same dashboard. Will also add a few descriptions and titles to make it more informative.",
                    "execution_mode": "sequential",
                    "details": {{}},
                    "action_end": true
                }}
            ]
        }}"""

        parser = JSONParser()

        text = f"""
        You are a data analyst specializing in data analytics, data engineering, data visualization, and data science.

        **Context**:
        - **Schemas**:
        {schemas}

        - **Previous messages**:
        {previous_messages}

        - **User's latest message**:
        {prompt}

        - **User's memories**:
        {f"Memories: {memories}" if memories else "No memories added"}

        - **Selected widget (if any)**:
        {f"{widget.id}\\n{widget.title}" if widget else "No widget selected"}

        {f"Selected widget data model:\n {step.data_model}" if step else "\n"}

        {observation_context}

        **Primary Task**:
        0. Think through this request step by step
        1. Identify if the user explicitly requests creating or modifying a widget. 
        - If the user only asks a clarifying question like "What other tables can be used?", do **not** modify or create a widget. 
            Instead, use only the 'answer_question' action.
        - If the user explicitly says "modify the widget" or "add columns from another table," then use 'modify_widget'.
        - If the user asks for any data listing like "list of customers" or "show me customers", treat this as a request to create a widget.
        2. If the user is just conversing or being friendly, respond with a single 'answer_question' action.
        3. If user is not specifically requesting a new chart, new table, or modification, do not create or modify widgets.
        4. If user only wants more information about existing data, respond with a single 'answer_question' action.
        5. Provide your plan as JSON with 'plan' as the top-level key, containing a list of actions.
        6. No code fences or markdown in the final output JSON.

        **Available Actions**:
        - answer_question
        - create_widget
        - modify_widget
        { "- design_dashboard" if self.organization_settings.get("ai_features", {}).get("dashboard_designer", {}).get("enabled", False) else "" }

        GUIDELINES
        - Make sure the user ask is legit. Do not support malicious requests or requests that involve leaking/writing data into the database.

        IMPORTANT PRE-CHECKS BEFORE CREATING A PLAN:
        1. If the user's message is simple greeting or thanks, just respond with a short 'answer_question' action. 
        2. If the question can be answered from the schemas and context alone, respond with a single 'answer_question' action.
        3. If a widget already exists that satisfies the user's goal, do not create a new one or modify it unnecessarily.
        4. Only create a new widget if it's clearly required by the user's request and there is no suitable existing widget.
        5. For metrics, create a widget per metric. Don't combine multiple metrics into a single widget.
        6. For "design_dashboard," do not recreate existing widgets. Combine them into a dashboard if they are relevant.
        7. Carefully verify all columns and data sources actually exist in the provided schemas.

        If you are responding after observing previous results:
        1. Analyze what was discovered in the previous step
        2. Determine if additional actions are needed
        3. If no further actions are needed, respond with an empty plan list and set "analysis_complete" to true
        4. If more actions are needed, provide them and set "requires_observation" to true for actions that require feedback

        1. **Determine the Nature of the Request**:
           Think step by step and reason through the user's request. 
           - If the user's message is a simple acknowledgment (like "thanks", "ok", "great") or greeting, 
             respond with a single `answer_question` action with a brief acknowledgment.
           - If the user asks to see, list, or display data from tables (like "show me customers", "list orders"), 
             use `create_widget` action to create a table or chart for better data visualization.
           - If the user's request can be answered directly from the given context (schemas, previous messages, memories), OR basic llm can answer the question (summarize, explain, etc.), 
             use `answer_question` action EVEN IF a widget is selected. Only use modify_widget if the user explicitly wants to change the widget.
           - If not directly answerable, generate a plan consisting of one or more actions. Actions can be:
                - "create_widget": For building tables, charts, or other data visuals from the schema.
                  * Avoid creating a 'create_widget' action if a widget is already selected in the context.*
                  * Do not create a 'create_widget' if an identical widget is already in the chat (previos messages).*
                - "modify_widget": For modifying an already existing widget's data model, chart type, or columns.
                  *IMPORTANT: Do not create a 'modify_widget' action if no widget is selected in the context.
                   was created in the current plan - instead, include all desired features in the initial create_widget action.*
                  *If a widget is selected, you can modify it. If no widget is selected, you must use create_widget.*
                - "answer_question": For answering user questions that are less for creating data/widgets/visuals.answering questions directly from context without data querying. 
                                    Good for summarizing, explaining, etc. 
                                    Or for conversing with the user. 
                                    If the user asks for advice, help in deciding what to do, what questions to asks, etc, you can answer that too.
                                    If it's greetings/conversing/etc, just respond briefly
                                    If you need to clarify a question, use this action.
                                    *IMPORTANT: If a widget is selected, bias your answer with widget context in mind.*
                - "design_dashboard": For creating a full, multi-step dashboard/report. If used, this should be the final action in the plan.
                  * if widget were already created and the request is to design a dashboard, simply just create a dashboard. 

        2. **When Generating a Plan**:
           - Begin your JSON output with the "analysis_complete" field to indicate whether the observation is finished and analysis complete, and no more actions are needed.
           - Provide a "reasoning" key that explains the thinking and the plan before execution.
           - Provide each action as a JSON object inside a "plan" array.
           - Each action must have:
             - "action": One of the defined actions.
             - "prefix": A short message that validates the user's request, and explains the thinking and the plan before execution. If you are creating a widget, explain how your building and modeling it. Also explain the reasoning behind the plan.
             - "execution_mode": Either "sequential" or "parallel". Use "parallel" if actions can be done independently. Otherwise, use "sequential".
             - "details": A dictionary of relevant details:
               * For "answer_question":
                 - "extracted_question": The question being answered (end with "$.")
               * For "create_widget":
                 - "title": The widget title, must end with "$."
                 - "data_model": A dictionary describing how to query and present the data.
                     - "type": The type of response ("table", "bar_chart", "line_chart", "pie_chart", "area_chart", "count", etc.)
                     - "columns": A list of columns in the data model. Each column is a dictionary with:
                       "generated_column_name", "source", and "description".
                     - For charts, include "series": a list where each item specifies:
                       "name": the series name,
                       "key": which column is used for categories,
                       "value": which column is used for the numeric values.
               * For "modify_widget":
                 - "data_model": (Optional) If you need to change the widget's underlying data model type or series:
                   - "type": New type of the widget if changing (e.g., from table to bar_chart).
                   - If changing or adding series (for charts), include "series".
                 - "remove_columns": A list of column names to remove.
                 - "add_columns": A list of new columns to add. Each column is a dictionary:
                     - "generated_column_name", "source", and "description".
                 - "transform_columns": A list of columns to transform. Each column is a dictionary:
                     - "generated_column_name", "source", and "description".
                 * Only include "data_model" or "series" if you intend to modify them. If not needed, omit them.
               * For "design_dashboard":
                 - Include details if needed. This should typically assemble multiple widgets.
             - "action_end": true (lowercase) at the end.

        **Data Model Guidelines**:
        - Review schemas, previous messages, and memories carefully.
        - Create a data model that conforms to the user's request.
        - You can add aggregations, derived columns, etc.
        - Keep the data model simple and concise.
        - Use ONLY columns that exist in the provided schemas or can be derived from them.
        - Derived columns or aggregations are allowed only if their source columns exist.
        - Respect data types: no numeric aggregations on non-numeric columns.
        - For charts, add "series" to define categories and values.
        - For counts, ensure a single numeric value.
        - No invented columns that aren't in schemas.

        **Output Format**:
        Begin your JSON output with the "analysis_complete" field to indicate whether the analysis is finished:
        {{
            "analysis_complete": false, // or true if analysis is complete
            "reasoning": "Your reasoning about the decision...",
            "plan": [
                // Your plan actions here
            ]
        }}
        - Return ONLY a valid JSON object with no explanatory text before or after. The response must begin with '{{' and end with '}}'."
        - Do not include any natural language explanations outside the JSON structure.
        - No markdown, no code fences in the final output.

        **Examples**:

        Example 1 (answer_question):
        {{
            "analysis_complete": true,
            "reasoning": "The user is asking about the data type of column X. I can answer this question by looking at the schema. Setting analysis_complete to true.",
            "plan": [
                {{
                    "action": "answer_question",
                    "prefix": "The type of column `X` is a string.", // always keep empty for answer_question
                    "execution_mode": "sequential",
                    "details": {{
                        "extracted_question": "What is the data type of column X?$."
                    }},
                    "action_end": true
                }}
            ]
        }}

        Example 2 (create_widget):
        {{  
            "analysis_complete": false,
            "reasoning": "The user is asking for a chart of revenue by month. I can create a bar chart with the month and total revenue coming from `sales` table joined with `payment` table and aggregate the data by month.",
            "plan": [
                {{
                    "action": "create_widget",
                    "prefix": "Let me prepare a chart for you. I will create a bar chart with the month and total revenue coming from `sales` table joined with `payment` table and aggregate the data by month.",
                    "execution_mode": "sequential",
                    "details": {{
                        "title": "Revenue by Month$.",
                        "data_model": {{
                            "type": "bar_chart",
                            "columns": [
                                {{
                                    "generated_column_name": "month",
                                    "source": "mydb.sales.month",
                                    "description": "Month of the sale as a string."
                                }},
                                {{
                                    "generated_column_name": "total_revenue",
                                    "source": "mydb.sales.amount",
                                    "description": "Sum of sales amounts per month."
                                }}
                            ],
                            "series": [
                                {{
                                    "name": "Monthly Revenue",
                                    "key": "month",
                                    "value": "total_revenue"
                                }}
                            ]
                        }}
                    }},
                    "action_end": true
                }}
            ]
        }}

        Example 3 (modify_widget ):
        {{
            "analysis_complete": false,
            "reasoning": "The user wants to modify the widget to remove `old_column` and add `new_column_name` that shows the total revenue per month and come from the `sales` table. I will also transform the `month` column to show the month as a number.",
            "plan": [
                {{
                    "action": "modify_widget",
                    "prefix": "Let me modify this widget for you. I will remove `old_column` and add `new_column_name` that shows the total revenue per month and come from the `sales` table. I will also transform the `month` column to show the month as a number.",
                    "execution_mode": "sequential",
                    "details": {{
                        "remove_columns": ["old_column"],
                        "add_columns": [
                            {{
                                "generated_column_name": "new_column_name", 
                                "source": "mydb.sales.new_column", 
                                "description": "New column description."
                            }}
                        ],
                        "transform_columns": [
                            {{
                                "generated_column_name": "transformed_column_name", 
                                "source": "mydb.sales.transformed_column", 
                                "description": "Transformed column description."
                            }}
                        ],
                        "data_model": {{
                            "type": "bar_chart",
                            "series": [
                                {{
                                    "name": "New Series Name", 
                                    "key": "month", 
                                    "value": "new_column_name"
                                }}
                            ]
                        }}
                    }},
                    "action_end": true
                }}
            ]
        }}

        {design_dashboard_example if self.organization_settings.get("ai_features", {}).get("dashboard_designer", {}).get("enabled", False) else ""}

        {{
            "analysis_complete": true,
            "reasoning": "I've analyzed all the widgets and found no further analysis needed. Setting analysis_complete to true.",
            "plan": []
        }}

        Now, based on the user's request and context, produce the final plan. Remember: no markdown, no code fences in your final output. 

        **CRITICAL INSTRUCTION**:
        Before creating any widgets, you MUST check if the widgets requested by the user ALREADY EXIST in the observation data.
        - If the requested widgets already exist → Set "analysis_complete" to TRUE and provide minimal actions
        - If any requested widget is missing → Set "analysis_complete" to FALSE and only create what's missing
        """

        # Example of completed analysis in prompt
        example_complete_analysis = """
        Example 5 (completed analysis):
        {
            "analysis_complete": true,
            "reasoning": "After analyzing the customer data, I've found that there are no duplicate customer records and all values are within expected ranges. No further investigation is needed.",
            "plan": [
                {
                    "action": "answer_question",
                    "prefix": "I've completed the analysis of the customer data and found no anomalies. All customer records are valid with an average age of 42 and an even distribution across regions.",
                    "execution_mode": "sequential",
                    "details": {
                        "extracted_question": "What did we learn from the customer data analysis?"
                    },
                    "action_end": true
                }
            ]
        }
        """

        # Example of continuing analysis in prompt
        example_continue_analysis = """
        Example 6 (continuing analysis):
        {
            "analysis_complete": false,
            "reasoning": "I've found an unusual pattern in customer spending. Several customers have identical purchase amounts on the same date, which might indicate duplicate transactions or a system error.",
            "plan": [
                {
                    "action": "create_widget",
                    "prefix": "I noticed an unusual pattern in the data. Let me investigate the transaction dates more closely.",
                    "execution_mode": "sequential",
                    "requires_observation": true,
                    "details": {
                        "title": "Duplicate Transaction Analysis",
                        "data_model": {
                            // Data model details here
                        }
                    },
                    "action_end": true
                }
            ]
        }
        """

        # Add examples to the prompt
        text += "\n" + example_complete_analysis + "\n" + example_continue_analysis
        
        # Count tokens in the prompt
        prompt_tokens = self.count_tokens(text)
        print(f"Prompt tokens: {prompt_tokens}")
        
        full_result = ""
        buffer = ""
        completion_tokens = 0
        current_plan = {"reasoning": "", "analysis_complete": False, "plan": [], "text": text}  # Initialize empty plan structure

        async for chunk in self.llm.inference_stream(text):
            
            buffer += chunk
            full_result += chunk
            completion_tokens += self.count_tokens(chunk)
            try:
                json_result = parser.parse(full_result)

                # Skip iteration if parsing failed or plan is missing
                if not json_result or not isinstance(json_result, dict) or "plan" not in json_result:
                    continue

                # IMPORTANT FIX: Explicitly extract and preserve analysis_complete flag
                if "analysis_complete" in json_result:
                    current_plan["analysis_complete"] = json_result["analysis_complete"]
                    
                    yield current_plan

                # Ensure reasoning is present
                if "reasoning" not in json_result:
                    json_result["reasoning"] = ""

                current_plan["reasoning"] = json_result["reasoning"]

                # Process each action using its index
                for action_index, action_item in enumerate(json_result["plan"]):
                    # Ensure current_plan["plan"] is long enough
                    while len(current_plan["plan"]) <= action_index:
                        current_plan["plan"].append({
                            "action": None,
                            "prefix": "",
                            "execution_mode": "sequential",
                            "details": {},
                            "action_end": False
                        })

                    current_action = current_plan["plan"][action_index]

                    # Update action if it's provided
                    if action_item.get("action") is not None:
                        current_action["action"] = action_item["action"]

                    # Update prefix if it has changed
                    if "prefix" in action_item and action_item["prefix"] != current_action["prefix"]:
                        current_action["prefix"] = action_item["prefix"]
                        yield current_plan

                    # Update execution_mode
                    if "execution_mode" in action_item:
                        current_action["execution_mode"] = action_item["execution_mode"]

                    # Mark action_end if provided
                    if "action_end" in action_item:
                        current_action["action_end"] = action_item["action_end"]
                        yield current_plan

                    # Process details if they exist and are not None
                    if "details" in action_item and action_item["details"] is not None:
                        details = action_item["details"]
                        current_details = current_action["details"]

                        # Handle extracted_question for answer_question action
                        if action_item["action"] == "answer_question" and "extracted_question" in details:
                            question = details["extracted_question"]
                            # Only update and yield if the question ends with "$."
                            if question and question.endswith("$."):
                                current_details["extracted_question"] = question[:-2]  # Remove the "$."
                                yield current_plan
                        # Handle design_dashboard action
                        elif action_item["action"] == "design_dashboard":
                            # For design_dashboard, we just need to ensure the action is marked as complete
                            if "action_end" in action_item and action_item["action_end"]:
                                # Update prefix if it has changed
                                if "prefix" in action_item and action_item["prefix"] != current_action["prefix"]:
                                    current_action["prefix"] = action_item["prefix"]
                                yield current_plan
                        # Update title
                        if "title" in details and details["title"] and details["title"].endswith("$."):
                            current_details["title"] = details["title"][:-2]

                        # Process data_model
                        if "data_model" in details:
                            data_model = details["data_model"]
                            if "data_model" not in current_details:
                                current_details["data_model"] = {"columns": [], "series": []}

                            # Update type in data_model
                            if "type" in data_model:
                                current_details["data_model"]["type"] = data_model["type"]

                            # Process columns
                            if "columns" in data_model and isinstance(data_model["columns"], list):
                                for column in data_model["columns"]:
                                    if not isinstance(column, dict):
                                        continue

                                    # Check if column is complete and not duplicate
                                    is_complete = (
                                        all(key in column for key in ['generated_column_name', 'source', 'description']) and
                                        isinstance(column['description'], str) and
                                        len(column['description'].strip()) > 0 and
                                        column['description'].strip().endswith('.') and
                                        not any(
                                            existing['generated_column_name'] == column['generated_column_name']
                                            for existing in current_details["data_model"]["columns"]
                                        )
                                    )

                                    if is_complete:
                                        current_details["data_model"]["columns"].append(column)
                                        yield current_plan

                            # Process series
                            if "series" in data_model and isinstance(data_model["series"], list):
                                # Check if all series items are complete and valid
                                series_complete = all(
                                    isinstance(series, dict) and
                                    all(key in series for key in ["name", "key", "value"]) and
                                    all(isinstance(series[key], str) for key in ["name", "key", "value"])
                                    for series in data_model["series"]
                                )
                                if series_complete:
                                    current_details["data_model"]["series"] = data_model["series"]
                                    yield current_plan

                        # Handle modify_widget specific details
                        if action_item["action"] == "modify_widget":
                            # Handle data_model if provided
                            if "data_model" in details:
                                data_model = details["data_model"]
                                if "data_model" not in current_details:
                                    current_details["data_model"] = {}
                                
                                # Update type if provided
                                if "type" in data_model:
                                    current_details["data_model"]["type"] = data_model["type"]
                                
                                # Update series if provided
                                if "series" in data_model and isinstance(data_model["series"], list):
                                    series_complete = all(
                                        isinstance(series, dict) and
                                        all(key in series for key in ["name", "key", "value"]) and
                                        all(isinstance(series[key], str) for key in ["name", "key", "value"])
                                        for series in data_model["series"]
                                    )
                                    if series_complete:
                                        current_details["data_model"]["series"] = data_model["series"]
                                        yield current_plan

                            # Handle remove_columns if provided
                            if "remove_columns" in details and isinstance(details["remove_columns"], list):
                                current_details["remove_columns"] = details["remove_columns"]
                                yield current_plan

                            # Handle add_columns if provided
                            if "add_columns" in details and isinstance(details["add_columns"], list):
                                if "add_columns" not in current_details:
                                    current_details["add_columns"] = []
                                
                                for column in details["add_columns"]:
                                    if not isinstance(column, dict):
                                        continue

                                    is_complete = (
                                        all(key in column for key in ['generated_column_name', 'source', 'description']) and
                                        isinstance(column['description'], str) and
                                        len(column['description'].strip()) > 10 and
                                        column['description'].strip().endswith('.') and
                                        not any(
                                            existing['generated_column_name'] == column['generated_column_name']
                                            for existing in current_details.get("add_columns", [])
                                        )
                                    )

                                    if is_complete:
                                        current_details["add_columns"].append(column)
                                        yield current_plan

                            # Handle transform_columns if provided
                            if "transform_columns" in details and isinstance(details["transform_columns"], list):
                                if "transform_columns" not in current_details:
                                    current_details["transform_columns"] = []
                                
                                for column in details["transform_columns"]:
                                    if not isinstance(column, dict):
                                        continue

                                    is_complete = (
                                        all(key in column for key in ['generated_column_name', 'source', 'description']) and
                                        isinstance(column['description'], str) and
                                        len(column['description'].strip()) > 10 and
                                        column['description'].strip().endswith('.') and
                                        not any(
                                            existing['generated_column_name'] == column['generated_column_name']
                                            for existing in current_details.get("transform_columns", [])
                                        )
                                    )

                                    if is_complete:
                                        current_details["transform_columns"].append(column)
                                        yield current_plan

            except Exception as e:
                print(f"Error processing JSON chunk: {e}")
                continue

            # Optionally, yield current_plan at the end of each chunk
            yield current_plan

        print("DEBUG: Streaming completed")  # This will show in your console logs
        
        # Final token counts
        print(f"Completion tokens: {completion_tokens}")
        print(f"Total tokens: {prompt_tokens + completion_tokens}")
        print(f"Reasoning: {current_plan['reasoning']}")
        
        # Modify the plan structure to include new fields
        final_plan = current_plan.copy()
        final_plan["streaming_complete"] = True
        final_plan["token_usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens
        }
        yield final_plan