from app.ai.llm import LLM
from partialjson.json_parser import JSONParser
from app.models.llm_model import LLMModel
from app.models.step import Step
from app.models.widget import Widget
from typing import List, Optional
import json

class DashboardDesigner:

    def __init__(self, model: LLMModel) -> None:
        self.llm = LLM(model)

    async def execute(self, prompt: str, widgets: List[Widget], steps: Optional[List[Step]], previous_messages: str):
        parser = JSONParser()
        current_design = {
            "prefix": "",
            "widgets": [],
            "text_widgets": [],
            "end_message": ""
        }

        detailed_widgets_parts = []
        widget_map = {}
        if widgets:
            for widget in widgets:
                widget_map[str(widget.id)] = widget
                widget_type = getattr(widget, 'type', 'unknown type')
                data_model = getattr(widget, 'data_model', None)
                if not widget_type or widget_type == 'unknown type':
                     if data_model and isinstance(data_model, dict):
                        widget_type = data_model.get('type', 'unknown type')

                columns_str = 'N/A'
                if data_model and isinstance(data_model, dict):
                     columns_list = data_model.get('columns', [])
                     if columns_list:
                         columns_str = ", ".join([c.get('generated_column_name', '?') for c in columns_list])

                detailed_widgets_parts.append(
                    f"Widget ID: {widget.id}\n"
                    f"  Title: {widget.title}\n"
                    f"  Type: {widget_type}\n"
                    f"  Columns/Data: {columns_str}"
                )
            detailed_widgets_str = "\n\n".join(detailed_widgets_parts)
        else:
            detailed_widgets_str = "No widgets provided for layout."

        steps_parts = []
        if steps:
             for i, step in enumerate(steps):
                 action = getattr(step, 'action', getattr(step, 'name', 'Unknown Action'))
                 prefix = getattr(step, 'prefix', 'No description provided.')
                 step_widget_id = getattr(step, 'widget_id', None)
                 step_widget_title = widget_map.get(str(step_widget_id), None)
                 step_widget_title = step_widget_title.title if step_widget_title else "N/A"

                 data_model_summary = ""
                 if hasattr(step, 'data_model') and step.data_model:
                     try:
                         dm_type = step.data_model.get('type', 'N/A')
                         dm_cols = [c.get('generated_column_name', 'N/A') for c in step.data_model.get('columns', [])]
                         data_model_summary = f"\n  Generated Data Type: {dm_type} | Columns: {', '.join(dm_cols)}"
                     except Exception:
                         data_model_summary = "\n  (Could not summarize data model)"

                 steps_parts.append(f"Step {i+1}: (Action: {action} | Widget: '{step_widget_title}' (ID: {step_widget_id}))\n  Details: {prefix}{data_model_summary}")
             steps_str = "\n\n".join(steps_parts)
        else:
            steps_str = "No analysis steps preceded this design request."

        text = f"""
        You are an expert dashboard / report analyst and designer. Your task is to create a dashboard layout based on a user's request, the available data widgets, the analysis steps performed, and the conversation history.
        The goal is NOT just to place widgets, but to arrange them and add explanatory **text widgets** to create a clear narrative that summarizes the analysis and directly addresses the user's initial prompt.

        **Context Provided**:

        1.  **User's Initial Prompt**:
            {prompt}

        2.  **Analysis Steps Taken**: (These explain *how* the widgets were created and *what* was done)
            {steps_str}

        3.  **Available Widgets & Data Context**: (These are the building blocks for the dashboard)
            {detailed_widgets_str}

        4.  **Conversation History (Previous Messages)**:
            {previous_messages}

        **Key Objectives**:
        1.  **Fulfill User Intent**: The dashboard layout MUST address the user's initial prompt, using the available widgets and insights from the analysis steps.
        2.  **Create a Narrative**: Use **text widgets** strategically. Start with an introductory text (title/summary). Place text widgets near related data widgets to explain what they show, summarize key findings from the analysis steps, and connect them back to the user's goal. The flow should tell a story.
        3.  **Layout & Storytelling**:
            - Arrange the provided data `widgets` logically to support the narrative.
            - Introduce the dashboard with an optional `text_widget` (e.g., `<h1>`).
            - Group related `widgets` and use `text_widgets` (e.g., `<p>`, `<h2>`) before or near them to explain their purpose and findings based on the "Analysis Steps Taken".
            - **Crucially, use the content of `text_widgets` to bridge the gap between the raw data widgets and the user's request, summarizing the analysis.**
        4.  **Text Widget Content**:
            - **Use HTML syntax** (e.g., `<h1>Title</h1>`, `<h2>Subtitle</h2>`, `<p>Paragraph</p>`, `<ul><li>List item</li></ul>`, `<a href="url">Link</a>`, `<table><tr><td>Cell</td></tr></table>`) inside the `content` field of `text_widgets`. Do NOT use Markdown syntax.
        5.  **Technical Constraints**:
            - **No Overlaps**: Absolutely no overlapping widgets (text or data).
            - **Grid System**: Max width = 1000px, max height = 3000px. Snap all x, y, width, height to a 20px grid (must be multiples of 20).
            - **Spacing**: Min 20px vertical spacing between stacked elements, min 10px horizontal between side-by-side.
            - **Data Widget Sizes**: Min 500x300px. Size appropriately, prefer wider aspect for charts.
            - **Text Widget Sizing**: Estimate height based on HTML content (e.g., h1 ≈ 40-60px, h2 ≈ 30-50px, paragraph line ≈ 20px). Ensure text widget height is a multiple of 20. Adjust height based on expected rendered lines.
        6.  **Output Format**:
            - Return JSON ONLY. No HTML formatting *outside* the `content` fields, no explanations outside the JSON structure.
            - Structure: `{{"prefix": "...", "widgets": [...], "text_widgets": [...], "end_message": "..."}}`
            - `widgets` array: Contains objects like `{{ "id": "UUID_from_Available_Widgets", "x": N, "y": N, "width": N, "height": N }}`. Use ONLY IDs from the "Available Widgets" section.
            - `text_widgets` array: Contains objects like `{{ "type": "text", "content": "<h1>Title</h1>...", "x": N, "y": N, "width": N, "height": N }}`. Content MUST be HTML.
            - `prefix`: Short loading message.
            - `end_message`: Short closing message, must end with `$.`.

        **Example (Conceptual - focus on text widgets with HTML)**:
        If steps involved creating a sales trend chart (UUID1) and a top products table (UUID2) in response to "Show me sales performance":
        {{
          "prefix": "Visualizing your sales performance analysis...",
          "widgets": [
            {{ "id": "UUID1", "x": 0, "y": 120, "width": 980, "height": 300 }}, // Main trend chart
            {{ "id": "UUID2", "x": 0, "y": 500, "width": 500, "height": 300 }}  // Top products table
          ],
          "text_widgets": [
            {{ // Report Title / Intro (HTML)
              "type": "text",
              "content": "<h1>Sales Performance Analysis</h1>\\n\\nThis dashboard summarizes the key findings regarding sales trends and top products based on your request.",
              "x": 0, "y": 0, "width": 980, "height": 100 // Approx H1 + 2 lines + spacing
            }},
            {{ // Explanation for Trend Chart (HTML)
              "type": "text",
              "content": "<h2>Monthly Sales Trend</h2>\\n\\nThe chart above shows a positive sales trend over the last quarter, peaking in December. *(Insight from Analysis Step 2)*",
              "x": 0, "y": 440, "width": 980, "height": 60 // Approx H2 + 2 lines
            }},
             {{ // Explanation for Top Products (HTML)
              "type": "text",
              "content": "<h2>Top Products</h2>\\n\\nThe table on the left lists the top 5 products driving revenue. **Product X** saw the most significant growth. *(Insight from Analysis Step 3)*",
              "x": 520, "y": 500, "width": 460, "height": 80 // Approx H2 + 2 lines
            }}
          ],
          "end_message": "Analysis dashboard complete$. "
        }}

        Now, based on the specific context (prompt, steps, available widgets, messages), generate the final JSON layout that places the provided widgets and uses new text widgets (with **HTML content**) to build a coherent analytical report narrative. Ensure all technical constraints are met.
        """

        full_result = ""

        async for chunk in self.llm.inference_stream(text):
            full_result += chunk

            try:
                json_result = parser.parse(full_result)

                if not json_result or not isinstance(json_result, dict):
                    continue

                update_yielded = False

                if "prefix" in json_result and json_result["prefix"] != current_design["prefix"]:
                    current_design["prefix"] = json_result["prefix"]
                    yield current_design
                    update_yielded = True

                if "widgets" in json_result and isinstance(json_result["widgets"], list):
                    new_widgets = []
                    changed = False
                    for widget_data in json_result["widgets"]:
                        required_fields = ["id", "x", "y", "width", "height"]
                        if (all(key in widget_data for key in required_fields) and
                            all(widget_data[key] is not None for key in required_fields) and
                            str(widget_data["id"]) in widget_map):

                            found = False
                            for i, existing_widget in enumerate(current_design["widgets"]):
                                if str(existing_widget["id"]) == str(widget_data["id"]):
                                    found = True
                                    if existing_widget != widget_data:
                                        current_design["widgets"][i] = widget_data
                                        changed = True
                                    break
                            if not found:
                                current_design["widgets"].append(widget_data)
                                changed = True

                    if changed:
                        yield current_design
                        update_yielded = True

                if "text_widgets" in json_result and isinstance(json_result["text_widgets"], list):
                    changed = False
                    processed_ids = set()

                    for widget_data in json_result["text_widgets"]:
                        required_fields = ["type", "content", "x", "y", "width", "height"]
                        if (all(key in widget_data for key in required_fields) and
                            all(widget_data[key] is not None for key in required_fields) and
                            widget_data["type"] == "text"):

                            widget_id = widget_data.get("id", hash(widget_data["content"]))
                            while widget_id in processed_ids:
                                widget_id = hash(str(widget_data["content"]) + str(widget_id))
                            processed_ids.add(widget_id)
                            widget_data["id"] = widget_id

                            found = False
                            for i, existing_widget in enumerate(current_design["text_widgets"]):
                                if existing_widget["id"] == widget_id:
                                    found = True
                                    if existing_widget != widget_data:
                                        current_design["text_widgets"][i] = widget_data
                                        changed = True
                                    break
                            if not found:
                                current_design["text_widgets"].append(widget_data)
                                changed = True

                    if changed:
                        yield current_design
                        update_yielded = True

                if "end_message" in json_result and json_result["end_message"].endswith("$."):
                    new_end_message = json_result["end_message"][:-2]
                    if new_end_message != current_design["end_message"]:
                         current_design["end_message"] = new_end_message
                         yield current_design
                         update_yielded = True

            except Exception as e:
                continue

        # The generator finishes here