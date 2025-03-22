from partialjson.json_parser import JSONParser
from app.ai.llm import LLM
from app.models.llm_model import LLMModel
import tiktoken  # Add this import for token counting

class Planner:

    def __init__(self, model: LLMModel) -> None:
        self.llm = LLM(model)

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

    async def execute(self, schemas, persona, prompt, memories, previous_messages, widget=None, step=None):
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

        **Primary Task**:
        1. Identify if the user explicitly requests creating or modifying a widget. 
        - If the user only asks a clarifying question like "What other tables can be used?", do **not** modify or create a widget. 
            Instead, use only the 'answer_question' action.
        - If the user explicitly says "modify the widget" or "add columns from another table," then use 'modify_widget'.
        2. If the user is just conversing or being friendly, respond with a single 'answer_question' action.
        3. If user is not specifically requesting a new chart, new table, or modification, do not create or modify widgets.
        4. If user only wants more information about existing data, respond with a single 'answer_question' action.
        5. Provide your plan as JSON with 'plan' as the top-level key, containing a list of actions.
        6. No code fences or markdown in the final output JSON.

        **Available Actions**:
        - answer_question
        - create_widget
        - modify_widget
        - design_dashboard

        IMPORTANT PRE-CHECKS BEFORE CREATING A PLAN:
        1. If the user's message is simple greeting or thanks, just respond with a short 'answer_question' action. 
        2. If the question can be answered from the schemas and context alone, respond with a single 'answer_question' action.
        3. If a widget already exists that satisfies the user's goal, do not create a new one or modify it unnecessarily.
        4. Only create a new widget if it's clearly required by the user's request and there is no suitable existing widget.
        5. For metrics, create a widget per metric. Don't combine multiple metrics into a single widget.
        6. For "design_dashboard," do not recreate existing widgets. Combine them into a dashboard if they are relevant.
        7. Carefully verify all columns and data sources actually exist in the provided schemas.


        1. **Determine the Nature of the Request**:
           - If the user's message is a simple acknowledgment (like "thanks", "ok", "great") or greeting, 
             respond with a single `answer_question` action with a brief acknowledgment.
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
           - Provide each action as a JSON object inside a "plan" array.
           - Each action must have:
             - "action": One of the defined actions.
             - "prefix": A short, kind message (1-2 sentences) shown before execution. If you are creating a widget, explain how your building and modeling it.
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
        - Return as a JSON object with a top-level "plan" key, containing a list of actions.
        - No markdown, no code fences in the final output.

        **Examples**:

        Example 1 (answer_question):
        {{
            "plan": [
                {{
                    "action": "answer_question",
                    "prefix": "", // always keep empty for answer_question
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
            "plan": [
                {{
                    "action": "create_widget",
                    "prefix": "Let me prepare a chart for you.",
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

        Example 3 (modify_widget):
        {{
            "plan": [
                {{
                    "action": "modify_widget",
                    "prefix": "Let me modify this widget for you.",
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

        Example 4 (design_dashboard):
        {{
            "plan": [
                {{
                    "action": "design_dashboard",
                    "prefix": "Finally, let's combine all insights into a dashboard.",
                    "execution_mode": "sequential",
                    "details": {{}},
                    "action_end": true
                }}
            ]
        }}

        Now, based on the user's request and context, produce the final plan. Remember: no markdown, no code fences in your final output. 
        """

        # Count tokens in the prompt
        prompt_tokens = self.count_tokens(text)
        print(f"Prompt tokens: {prompt_tokens}")
        
        full_result = ""
        buffer = ""
        completion_tokens = 0

        current_plan = {"plan": [], "text": text}  # Initialize empty plan structure

        async for chunk in self.llm.inference_stream(text):
            buffer += chunk
            full_result += chunk
            completion_tokens += self.count_tokens(chunk)

            try:
                json_result = parser.parse(full_result)

                # Skip iteration if parsing failed or plan is missing
                if not json_result or not isinstance(json_result, dict) or "plan" not in json_result:
                    continue

                # Ensure plan is a list
                if not isinstance(json_result["plan"], list):
                    continue

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
                                        len(column['description'].strip()) > 10 and
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

        # Add a final yield with a special flag or breakpoint here
        print("DEBUG: Streaming completed")  # This will show in your console logs
        
        # Final token counts
        print(f"Completion tokens: {completion_tokens}")
        print(f"Total tokens: {prompt_tokens + completion_tokens}")
        
        # Add token counts to the final plan
        final_plan = current_plan.copy()
        final_plan["streaming_complete"] = True
        final_plan["token_usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens
        }

        yield final_plan
        
        # For debugging with breakpoints, you can add:
        # import pdb; pdb.set_trace()  # This will pause execution here when reached