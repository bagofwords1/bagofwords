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
        The goal is NOT just to place widgets, but to arrange them **thoughtfully** and add explanatory **text widgets** to create a clear, compelling narrative that summarizes the analysis and directly addresses the user's initial prompt.

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
        1.  **Fulfill User Intent**: The layout MUST address the user's initial prompt, using the available widgets and insights from the analysis steps.
        2.  **Create a Narrative**: Use **text widgets** strategically. Start with an introductory text (title/summary). Place text widgets near related data widgets to explain *what* they show, summarize key findings from the analysis steps, and connect them back to the user's goal. The flow should tell a story.
        3.  **Layout & Storytelling**:
            - **Arrange widgets logically to tell a story.** Consider the flow of analysis. Start broad (summary/KPIs), then dive deeper.
            - **Don't just stack everything vertically.** Create a visually engaging layout. Use side-by-side placement (mosaic style) where it makes sense to group related smaller widgets or compare visuals. For instance, a KPI widget might sit next to the introductory text for the chart it relates to.
            - **Size matters.** Allocate space based on importance and content complexity. Key charts might span the full width (12 columns), while secondary charts or tables could share a row (e.g., two 6-column widgets). Use `text_widgets` effectively to introduce sections or individual widgets.
            - Introduce the dashboard with an optional `text_widget` (e.g., `<h1>`).
            - Group related `widgets` and use `text_widgets` (e.g., `<p>`, `<h2>`) positioned **directly before** the related data widget(s) they describe.
            - **Crucially, use the content of `text_widgets` to bridge the gap between the raw data widgets and the user's request, summarizing the analysis.**
        4.  **Text Widget Content**:
            - **Use HTML syntax** (e.g., `<h1>Title</h1>`, `<h2>Subtitle</h2>`, `<p>Paragraph</p>`, `<ul><li>List item</li></ul>`, `<a href="url">Link</a>`, `<table><tr><td>Cell</td></tr></table>`) inside the `content` field of `text_widgets`. Do NOT use Markdown syntax.
        5.  **Technical Constraints (Grid System)**:
            - **Grid**: Use a 12-column grid system (columns indexed 0-11).
            - **Coordinates & Dimensions**:
                - `x`: Starting column index (0-11).
                - `y`: Starting row index (absolute, starting from 0). Rows define vertical position.
                - `width`: Number of columns spanned (1-12).
                - `height`: Number of rows spanned (minimum 1).
            - **CRITICAL**: All `x`, `y`, `width`, `height` values MUST be small integer grid units based on the 12-column grid, NOT pixel values. Values larger than 12 for `x` or `width`, or very large values for `y` or `height` (e.g., > 50), are incorrect and invalid.
            - **No Overlaps**: Ensure no widgets (text or data) overlap in the grid. Check `y` and `y + height` for vertical overlaps, and `x` and `x + width` for horizontal overlaps within the same row span.
            - **Data Widget Sizes**: Minimum `width` of 4-6 columns (adjust based on content), minimum `height` of 5 rows. Size appropriately (charts often need `height` 8-12+ rows; tables vary).
            - **Text Widget Sizing**: Determine `height` in rows based on the **rendered HTML content** and the **chosen `width`**. The goal is to allocate enough vertical grid space for the text to be fully readable without excessive scrolling within the widget.
                - **Content Complexity**: Consider the amount and type of content (headings, paragraphs, lists, tables, etc.). More complex or longer content needs more height.
                - **Width is Crucial**: Text wraps. A narrow `width` (e.g., 4-6 columns) will require **significantly more `height`** than a wide `width` (e.g., 10-12 columns) for the *same* content.
                - **Estimation Process**:
                    1. Estimate the vertical space needed for the content assuming a wide layout (e.g., 10-12 columns). Think in terms of approximate lines of text or visual blocks.
                    2. **Adjust significantly upwards** for narrower widths. The narrower the widget, the more rows are needed. For very narrow widgets (< 7 columns), the height might need to be doubled or tripled compared to a wide layout estimate, depending on content density.
                - **Minimum Height**: Ensure the final `height` is at least 2 rows. **Never use `height: 1` for anything more than a single, short heading, especially with narrow widths.** Ensure `width` is also reasonable (usually >= 4 columns). A single short heading might fit `height: 1`, but a heading plus even a short paragraph usually needs `height: 2` *at minimum*, and more if the width is constrained.
                - Be EXTRA generous with height for text widgets (at least 2x compared to your initial estimate).
        6.  **Output Format**:
            - Return JSON ONLY. No explanations outside the JSON structure.
            - Structure: `{{"prefix": "...", "widgets": [...], "text_widgets": [...], "end_message": "..."}}`
            - `widgets` array: Contains objects like `{{ "id": "UUID_from_Available_Widgets", "x": N, "y": N, "width": N, "height": N }}`. Use ONLY IDs from "Available Widgets". `x`, `y`, `width`, `height` are grid units.
            - `text_widgets` array: Contains objects like `{{ "type": "text", "content": "HTML...", "x": N, "y": N, "width": N, "height": N }}`. Content MUST be HTML. `x`, `y`, `width`, `height` are grid units.
            - `prefix`: Short loading message.
            - `end_message`: Short closing message, must end with `$.`.

        **Example (Conceptual - Mosaic Layout)**:
        Showing Sales Trend (UUID1), Top Products Table (UUID2), and a KPI Card (UUID3).
        {{
          "prefix": "Visualizing your sales performance...",
          "widgets": [
             // Trend Chart (Full Width) starts after Intro Text + 1 empty row
            {{ "id": "UUID1", "x": 0, "y": 3, "width": 12, "height": 8 }}, // Rows 3-10
             // KPI Card (Left Half) starts after Trend Chart + 1 empty row
            {{ "id": "UUID3", "x": 0, "y": 13, "width": 5, "height": 4 }}, // Rows 13-16
             // Top Products Table (Right Half) starts after Trend Chart + 1 empty row, next to Text
            {{ "id": "UUID2", "x": 6, "y": 13, "width": 6, "height": 6 }}  // Rows 13-18
          ],
          "text_widgets": [
            {{ // Report Title / Intro (HTML)
              "type": "text",
              "content": "<h1>Sales Performance Analysis</h1><p>Summary...</p>",
              "x": 0, "y": 0, "width": 12, "height": 2 // Rows 0-1. Row 2 is empty.
            }},
            {{ // Explanation for KPI + Table (Spans width below chart)
              "type": "text",
              "content": "<h2>Key Metrics & Top Products</h2><p>The KPI card highlights total revenue. The table details top products.</p>",
              // Starts after Chart (Row 10) + 1 empty row = Row 11
              "x": 0, "y": 11, "width": 12, "height": 2 // Rows 11-12. Widgets start Row 13.
            }}
            // Note: Could optionally have placed text specifically for the table at x: 6, y: 11, width: 6, height: 2
          ],
          "end_message": "Analysis dashboard complete$. "
        }}

        Now, based on the specific context (prompt, steps, available widgets, messages), generate the final JSON layout. Prioritize creating a **visually appealing and narrative-driven layout** using mosaic arrangements where appropriate. Ensure all technical constraints (**especially the grid unit requirement and spacing rules**) are met.
        """

        full_result = ""

        async for chunk in self.llm.inference_stream(text):
            full_result += chunk
            # ---- TEMPORARY LOGGING ----
            # print("--- RAW LLM CHUNK ---")
            # print(chunk)
            # print("--- CURRENT FULL RESULT ---")
            # print(full_result)
            # ---- END LOGGING ----
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