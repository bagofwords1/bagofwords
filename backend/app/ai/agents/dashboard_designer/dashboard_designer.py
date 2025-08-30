from app.ai.llm import LLM
from partialjson.json_parser import JSONParser
from app.models.llm_model import LLMModel
from app.models.step import Step
from app.models.widget import Widget
from typing import List, Optional
from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder
import json

class DashboardDesigner:

    def __init__(self, model: LLMModel, instruction_context_builder: InstructionContextBuilder) -> None:
        self.llm = LLM(model)
        self.instruction_context_builder = instruction_context_builder

    async def execute(self, prompt: str, widgets: List[Widget], steps: Optional[List[Step]], previous_messages: str):
        parser = JSONParser()
        current_design = {
            "prefix": "",
            "blocks": [],
            "end_message": ""
        }
        processed_block_ids = set()

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

        # Build instruction context using the standard ContextHub builder pattern
        inst_section = await self.instruction_context_builder.build()
        instructions_context = inst_section.render()
        breakpoint()

        text = f"""
        You are an expert dashboard / report analyst and designer. Your task is to create a dashboard layout based on a user's request, the available data widgets, the analysis steps performed, and the conversation history.
        The goal is NOT just to place widgets, but to arrange them **thoughtfully** and add explanatory **text widgets** to create a clear, compelling narrative that summarizes the analysis and directly addresses the user's initial prompt.

        **General Organization Instructions**:
        **VERY IMPORTANT, CREATED BY THE USER, MUST BE USED AND CONSIDERED**:
        {instructions_context}

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
            - **Don't just stack everything vertically.** Create a visually engaging layout. Use side-by-side placement (mosaic style) where it makes sense to group related smaller widgets or compare visuals.
            - **Size matters.** Allocate space based on importance and content complexity. Key charts might span the full width (12 columns), while secondary charts or tables could share a row (e.g., two 6-column widgets).
            - Introduce the dashboard with an optional text widget (`type: "text"`, e.g., using `<h1>`).
            - Group related data widgets (`type: "widget"`) and use text widgets (`type: "text"`, e.g., using `<p>`, `<h2>`) positioned **directly before** the related data widget(s) they describe.
            - **Crucially, use the content of text widgets to bridge the gap between the raw data widgets and the user's request, summarizing the analysis.**
        4.  **Text Widget Content**:
            - **Use HTML syntax** (e.g., `<h1>Title</h1>`, `<h2>Subtitle</h2>`, `<p>Paragraph</p>`, `<ul><li>List item</li></ul>`, `<a href="url">Link</a>`, `<table><tr><td>Cell</td></tr></table>`) inside the `content` field of text widgets. Do NOT use Markdown syntax.
        5.  **Technical Constraints (Grid System)**:
            - **Grid**: Use a 12-column grid system (columns indexed 0-11).
            - **Coordinates & Dimensions**:
                - `x`: Starting column index (0-11).
                - `y`: Starting row index (absolute, starting from 0). Rows define vertical position.
                - `width`: Number of columns spanned (1-12).
                - `height`: Number of rows spanned (minimum 1).
            - **CRITICAL**: All `x`, `y`, `width`, `height` values MUST be small integer grid units based on the 12-column grid, NOT pixel values. Values larger than 12 for `x` or `width`, or very large values for `y` or `height` (e.g., > 50), are incorrect and invalid.
            - **No Overlaps**: Ensure no blocks (text or data) overlap in the grid. Check `y` and `y + height` for vertical overlaps, and `x` and `x + width` for horizontal overlaps within the same row span.
            - **Data Widget Sizes**: Minimum `width` of 4-6 columns (adjust based on content), minimum `height` of 5 rows. Size appropriately (charts often need `height` 8-12+ rows; tables vary). Use the `id` from the "Available Widgets" list.
            - **Text Widget Sizing**: Determine `height` in rows based on the **rendered HTML content** and the **chosen `width`**. A narrow `width` requires **significantly more `height`**. Be generous (at least 2 rows minimum, often more).
        6.  **Output Format**:
            - Return JSON ONLY. No explanations outside the JSON structure.
            - Structure: `{{"prefix": "...", "blocks": [...], "end_message": "..."}}`
            - `blocks` array: Contains **ordered** objects representing either data widgets or text widgets.
                - Data Widget Block: `{{ "type": "widget", "id": "UUID_from_Available_Widgets", "x": N, "y": N, "width": N, "height": N }}`. Use ONLY IDs from "Available Widgets". `x`, `y`, `width`, `height` are grid units.
                - Text Widget Block: `{{ "type": "text", "content": "HTML...", "x": N, "y": N, "width": N, "height": N }}`. Content MUST be HTML. `x`, `y`, `width`, `height` are grid units. Assign a temporary unique ID (e.g., "text_block_1") if needed internally for streaming updates, but it's not strictly required in the final output structure itself.
            - `prefix`: Short loading message.
            - `end_message`: Short closing message, must end with `$.`.

        **Example (Conceptual - Mosaic Layout)**:
        Showing Sales Trend (UUID1), Top Products Table (UUID2), and a KPI Card (UUID3).
        {{
          "prefix": "Visualizing your sales performance...",
          "blocks": [
            {{ // Report Title / Intro (HTML)
              "type": "text",
              "content": "<h1>Sales Performance Analysis</h1><p>Summary...</p>",
              "x": 0, "y": 0, "width": 12, "height": 2 // Rows 0-1. Row 2 is empty.
            }},
            {{ // Trend Chart (Full Width) starts after Intro Text + 1 empty row
              "type": "widget", "id": "UUID1", "x": 0, "y": 3, "width": 12, "height": 8 // Rows 3-10
            }},
            {{ // Explanation for KPI + Table (Spans width below chart)
              "type": "text",
              "content": "<h2>Key Metrics & Top Products</h2><p>The KPI card highlights total revenue. The table details top products.</p>",
              // Starts after Chart (Row 10) + 1 empty row = Row 11
              "x": 0, "y": 11, "width": 12, "height": 2 // Rows 11-12. Widgets start Row 13.
            }},
             {{ // KPI Card (Left Half) starts after Trend Chart + 1 empty row
              "type": "widget", "id": "UUID3", "x": 0, "y": 13, "width": 5, "height": 4 // Rows 13-16
            }},
             {{ // Top Products Table (Right Half) starts after Trend Chart + 1 empty row, next to Text
              "type": "widget", "id": "UUID2", "x": 6, "y": 13, "width": 6, "height": 6 // Rows 13-18
            }}
          ],
          "end_message": "Analysis dashboard complete$. "
        }}

        Now, based on the specific context (prompt, steps, available widgets, messages), generate the final JSON layout. Prioritize creating a **visually appealing and narrative-driven layout** using mosaic arrangements where appropriate. Ensure all technical constraints (**especially the grid unit requirement and spacing rules**) are met. Stream the JSON structure, updating the `blocks` list incrementally.
        """

        full_result = ""
        last_yielded_design_str = ""

        async for chunk in self.llm.inference_stream(text):
            full_result += chunk
            try:
                json_result = parser.parse(full_result)

                if not json_result or not isinstance(json_result, dict):
                    continue

                update_occurred = False

                # 1. Update Prefix
                new_prefix = json_result.get("prefix")
                if new_prefix is not None and new_prefix != current_design["prefix"]:
                    current_design["prefix"] = new_prefix
                    update_occurred = True

                # 2. Update Blocks (Incrementally)
                if "blocks" in json_result and isinstance(json_result["blocks"], list):
                    blocks_changed = False
                    temp_current_blocks = list(current_design["blocks"])

                    for index, block_data in enumerate(json_result["blocks"]):
                        if not isinstance(block_data, dict): continue

                        block_type = block_data.get("type")
                        layout_fields = ["x", "y", "width", "height"]

                        # Basic validation
                        if not block_type or not all(key in block_data for key in layout_fields) or \
                           not all(isinstance(block_data[key], int) for key in layout_fields):
                            continue # Skip invalid blocks

                        # Assign/Get unique ID for comparison
                        block_id = None
                        if block_type == "widget":
                            widget_id_str = str(block_data.get("id"))
                            if widget_id_str in widget_map:
                                block_id = widget_id_str # Use widget UUID as ID
                            else:
                                continue # Skip widget block if ID is invalid/missing
                        elif block_type == "text":
                            # Generate a pseudo-stable ID for text blocks based on order and content hash
                            content_hash = hash(block_data.get("content", ""))
                            block_id = f"text_{index}_{content_hash}"
                        else:
                            continue # Skip unknown block types

                        # Add 'internal_id' for tracking during streaming
                        block_data_with_id = {**block_data, "internal_id": block_id}

                        # Check if this block (by internal_id) already exists or needs update
                        found_index = -1
                        for i, existing_block in enumerate(temp_current_blocks):
                            if existing_block.get("internal_id") == block_id:
                                found_index = i
                                break

                        if found_index != -1:
                            # Update existing block if different
                            if temp_current_blocks[found_index] != block_data_with_id:
                                temp_current_blocks[found_index] = block_data_with_id
                                blocks_changed = True
                        else:
                            # Add new block if it wasn't found
                            temp_current_blocks.append(block_data_with_id)
                            blocks_changed = True

                    # Update the main design if changes occurred
                    if blocks_changed:
                        current_design["blocks"] = temp_current_blocks
                        update_occurred = True


                # 3. Update End Message
                new_end_message_raw = json_result.get("end_message")
                if new_end_message_raw is not None and new_end_message_raw.endswith("$."):
                    new_end_message = new_end_message_raw[:-2].strip() # Remove suffix and strip whitespace
                    if new_end_message != current_design["end_message"]:
                         current_design["end_message"] = new_end_message
                         update_occurred = True

                # Yield only if an update occurred and the design is different from the last yield
                current_design_str = json.dumps(current_design, sort_keys=True)
                if update_occurred and current_design_str != last_yielded_design_str:
                    yield current_design
                    last_yielded_design_str = current_design_str

            except Exception as e:
                # Log parsing errors if needed, but continue stream
                # print(f"Partial JSON parsing error: {e}")
                continue

        # Final yield with potentially completed end_message if not yielded before
        current_design_str = json.dumps(current_design, sort_keys=True)
        if current_design_str != last_yielded_design_str:
             yield current_design

        # The generator finishes here