from app.ai.llm import LLM
from partialjson.json_parser import JSONParser

from app.models.llm_model import LLMModel

class DashboardDesigner:

    def __init__(self, model: LLMModel) -> None:
        self.llm = LLM(model)

    async def execute(self, prompt, widgets, steps, previous_messages):
        parser = JSONParser()
        current_design = {
            "prefix": "",
            "widgets": [],
            "text_widgets": [],
            "end_message": ""
        }

        text = f"""
        You are an expert dashboard UI/UX designer. Given a user prompt, a list of widgets (charts, tables, etc.), and previous context, produce a JSON layout for a dashboard. The final output must be a JSON object describing widget placements and optional text widgets that provide structure and context.

        Widgets:
        {'\n'.join([f"Widget {widget.id}: {widget.title}" for widget in widgets])}

        previous messages:
        {previous_messages}

        User initial prompt:
        {prompt}


        **Key Objectives**:
        1. **Fulfill User Intent**: The dashboard should address the user's initial prompt and align with any relevant previous messages.
        2. **Layout & Storytelling**: Arrange widgets to form a logical narrative. Introduce the dashboard with an optional text widget (like a title or short description). Group related widgets together, and use text widgets before or near them to explain their purpose.
        3. **No Overlaps**: Absolutely no overlapping of widgets. Carefully compute positions (x,y) and sizes (width,height) so every element fits without collision.
        4. **Consistent Spacing & Sizing**:
           - Coordinate system starts at x=0, y=0.
           - Max dashboard width = 1000px, max height = 3000px.
           - Use a 20px grid snap for all positions and sizes (all x,y,width,height should be multiples of 20).
           - Vertical spacing: At least 20px between stacked widgets.
           - Horizontal spacing: At least 10px between side-by-side widgets.
           - Widgets (charts/tables) min size: 500px width, 300px height.
           - Avoid making widgets excessively large; keep them appropriately sized.
           - For charts like bar/line charts, prefer a wider aspect.
           - For minimal data displays (like simple counts), use smaller widgets.
        5. **Text Widgets**:
           - Use text widgets for titles, sections, short explanations. Keep text concise and helpful.
           - Text widgets can contain HTML (e.g., <h1>, <h2>, <p>).
           - Height calculation for text:
             - h1 line ≈ 60px
             - p or simple text line ≈ 20px each
             Example: A widget with an h1 and 2 lines of text might need around 100-120px.
           - All text widgets must also align to the 20px grid.
        6. **Styling Considerations**:
           - Start with a brief introductory text widget at the top if needed.
           - Order widgets so the user can understand the data story:
             - Introduce the topic (text widget, maybe h1 or h2),
             - Present the key insights (charts/tables),
             - Optionally add explanatory text near complex charts.
           - Place related widgets near each other for coherence.
        7. **Semantics & User Prompt Alignment**:
           - Reflect the user's intent in layout and sizing.
           - Consider making a main chart prominent if it’s central to the prompt.
        8. **Prefix and End Message**:
           - "prefix": A short message shown when the dashboard loads.
           - "end_message": A closing message, must end with "$.".
        9. **Output Format**:
           - Return JSON only, no markdown or code fences.
           - Example structure:
             {{
               "prefix": "Welcome to your dashboard!",
               "widgets": [
                 {{ "id": "UUID", "x": 0, "y": 0, "width": 500, "height": 300 }},
                 ...
               ],
               "text_widgets": [
                 {{ "type": "text", "content": "<h1>Dashboard Title</h1>", "x":0, "y":320, "width":500, "height":120 }},
                 ...
               ],
               "end_message": "All set!$."
             }}

        10. **No Extra Formatting**:
            - Start response directly with the JSON object (no Markdown, no code fences).
            - Ensure all coordinates and sizes are multiples of 20.
            - Ensure no overlaps.

        **Additional Examples**:

        Example 1 (Simple Dashboard):
        {{
          "prefix": "Loading your data visualization...",
          "widgets": [
            {{
              "id": "UUID1",
              "x": 0,
              "y": 140,
              "width": 500,
              "height": 300
            }}
          ],
          "text_widgets": [
            {{
              "type": "text",
              "content": "<h1>Sales Overview</h1><p>This chart shows monthly sales trends.</p>",
              "x": 0,
              "y": 0,
              "width": 500,
              "height": 120
            }}
          ],
          "end_message": "Dashboard fully loaded$."
        }}

        In this example:
        - The text widget is at y=0, height=120px to accommodate h1 + p lines.
        - The chart starts at y=140 (120 + 20px vertical spacing).
        - Everything aligns to multiples of 20 and no overlap occurs.

        Example 2 (More Complex Dashboard):
        {{
          "prefix": "Welcome to the analytics dashboard!",
          "widgets": [
            {{
              "id": "UUID2",
              "x": 0,
              "y": 140,
              "width": 500,
              "height": 300
            }},
            {{
              "id": "UUID3",
              "x": 520,
              "y": 140,
              "width": 500,
              "height": 300
            }}
          ],
          "text_widgets": [
            {{
              "type": "text",
              "content": "<h1>Company Performance Overview</h1><p>Below you can see our sales trends and revenue breakdown.</p>",
              "x": 0,
              "y": 0,
              "width": 1000,
              "height": 120
            }},
            {{
              "type": "text",
              "content": "<p>The line chart on the left shows monthly sales units, while the bar chart on the right breaks down revenue by category.</p>",
              "x": 0,
              "y": 460,
              "width": 1000,
              "height": 60
            }}
          ],
          "end_message": "All set and ready to explore!$."
        }}

        In this more complex example:
        - Intro text spans full width (1000px) and sits at the top.
        - Two charts placed side by side with a 20px gap (0 to 500, then 520 to 1020).
        - A second text widget placed below the charts at y=460 (charts end at 440 + 20px gap).
        - All coordinates and sizes are multiples of 20. No overlaps.

        Use these examples as a reference when producing the final layout.

        Now, produce the final JSON layout following all these guidelines based on the given prompt, widgets, and context.
        """

        full_result = ""
        
        async for chunk in self.llm.inference_stream(text):
            full_result += chunk
            
            try:
                json_result = parser.parse(full_result)

                # Skip iteration if parsing failed or required fields missing
                if not json_result or not isinstance(json_result, dict):
                    continue

                # Update prefix if it exists and has changed
                if "prefix" in json_result and json_result["prefix"] != current_design["prefix"]:
                    current_design["prefix"] = json_result["prefix"]
                    yield current_design

                # Process regular widgets array
                if "widgets" in json_result and isinstance(json_result["widgets"], list):
                    for widget in json_result["widgets"]:
                        required_fields = ["id", "x", "y", "width", "height"]
                        if (all(key in widget for key in required_fields) and 
                            all(widget[key] is not None for key in required_fields)):
                            if not any(existing_widget["id"] == widget["id"] for existing_widget in current_design["widgets"]):
                                current_design["widgets"].append(widget)
                                yield current_design
                            else:
                                for i, existing_widget in enumerate(current_design["widgets"]):
                                    if existing_widget["id"] == widget["id"]:
                                        if existing_widget != widget:
                                            current_design["widgets"][i] = widget
                                            yield current_design
                                        break

                # Process text widgets array
                if "text_widgets" in json_result and isinstance(json_result["text_widgets"], list):
                    for widget in json_result["text_widgets"]:
                        required_fields = ["type", "content", "x", "y", "width", "height"]
                        if (all(key in widget for key in required_fields) and 
                            all(widget[key] is not None for key in required_fields)):
                            # Generate a unique ID for text widgets based on content if not present
                            widget_id = widget.get("id", hash(widget["content"]))
                            widget["id"] = widget_id
                            
                            if not any(existing_widget["id"] == widget_id for existing_widget in current_design["text_widgets"]):
                                current_design["text_widgets"].append(widget)
                                yield current_design
                            else:
                                for i, existing_widget in enumerate(current_design["text_widgets"]):
                                    if existing_widget["id"] == widget_id:
                                        if existing_widget != widget:
                                            current_design["text_widgets"][i] = widget
                                            yield current_design
                                        break

                # Update end_message if it exists and ends with "$."
                if "end_message" in json_result and json_result["end_message"].endswith("$."):
                    current_design["end_message"] = json_result["end_message"][:-2]  # Remove "$."
                    yield current_design

            except Exception as e:
                print(f"Error processing JSON chunk: {e}")
                continue

            yield current_design