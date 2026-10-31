"""Live-model check of upload/capture ordering using synthetic chat screenshots.

Run from backend with ANTHROPIC_API_KEY and PLANNER_PROBE_MODEL in the environment.
No application data, artifact writes, or browser sessions are involved.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from PIL import Image, ImageDraw

from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
from app.ai.context.parts import ToolCallPart, ToolResultPart
from app.ai.context.transcript import Transcript
from app.ai.llm.clients.anthropic_client import Anthropic
from app.ai.llm.types import ImageInput, Message, ToolSpec, TextDeltaEvent, ToolUseCompleteEvent
from app.schemas.ai.planner import PlannerInput, ToolDescriptor


def screenshot(table: bool) -> ImageInput:
    image = Image.new("RGB", (640, 480), "#f7f6fc")
    draw = ImageDraw.Draw(image)
    draw.rectangle((25, 25, 615, 85), fill="#6843c2")
    draw.text((45, 45), "Music Store Assistant", fill="white", font_size=24)
    draw.text((45, 115), "You: Who are our top artists?", fill="#222222", font_size=20)
    draw.text((45, 160), "Assistant: Artist A leads, followed by Artist B.", fill="#222222", font_size=20)
    if table:
        for y, text in [(220, "ARTIST             SALES"), (260, "Artist A           120"), (300, "Artist B            80")]:
            draw.rectangle((45, y, 595, y + 39), outline="#888888", fill="white")
            draw.text((60, y + 8), text, fill="#222222", font_size=18)
    draw.rectangle((45, 400, 595, 450), outline="#6843c2", fill="white")
    draw.text((60, 415), "Ask a question...", fill="#555555", font_size=18)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return ImageInput(data=base64.b64encode(out.getvalue()).decode())


async def main():
    upload, current = screenshot(True), screenshot(False)
    live = Transcript()
    for call_id, name, result, images in [
        ("edit-1", "edit_artifact", {"success": True, "artifact_id": "chat", "version": 2,
                                    "code": "Render message text only; no result tables."}, []),
        ("view-2", "browser_navigate", {"success": True, "artifact": {"id": "chat", "version": 2, "code_hash": "text-only"},
                                       "snapshot": "Chat open. User question and assistant text answer. No table rendered."}, [vars(current)]),
    ]:
        live.add_assistant_step(calls=[ToolCallPart(id=call_id, tool_name=name, args={"artifact_id": "chat"})])
        live.add_tool_results([ToolResultPart(call_id=call_id, tool_name=name,
                                             content=json.dumps(result), images=images)])
    catalog = [ToolDescriptor(name=name, description=description, schema={"type": "object", "properties": {}})
               for name, description in [("edit_artifact", "Edit the chat artifact if a further correction is needed."),
                                         ("browser_navigate", "Inspect the latest saved chat artifact.")]]
    built = PromptBuilderV3.build(PlannerInput(
        user_message="Remove the table from this chat. Keep the text answer.",
        images=[upload], transcript=live, tool_catalog=catalog, timezone="UTC",
        organization_name="Synthetic test", organization_ai_analyst_name="Assistant",
        use_transcript=True,
    ))
    model = os.environ["PLANNER_PROBE_MODEL"]
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    client.max_tokens = 1600
    text, calls = [], []
    async for event in client.inference_stream_v2(
        model_id=model, system=built.system,
        messages=[Message(**m) for m in built.messages], images=built.images,
        tools=[ToolSpec(**t) for t in built.tools],
    ):
        if isinstance(event, TextDeltaEvent):
            text.append(event.text)
        elif isinstance(event, ToolUseCompleteEvent):
            calls.append(event.name)
    print(json.dumps({"model": model, "tool_calls": calls, "reply": "".join(text)}, indent=2))
    if calls or not text:
        raise SystemExit("Inspect output: the planner did not finish after the updated capture.")


if __name__ == "__main__":
    asyncio.run(main())
