import asyncio
from typing import AsyncIterator, Dict, Any

from app.ai.tools.base import Tool
from app.ai.schemas.tools.answer_question import AnswerQuestionInput, AnswerQuestionOutput


class AnswerQuestionTool(Tool):
    name = "answer_question"
    description = "Answer directly from context with a concise response."
    input_model = AnswerQuestionInput
    output_model = AnswerQuestionOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        # Validate input via schema (lightweight)
        data = AnswerQuestionInput(**tool_input)
        yield {"type": "tool.start", "payload": {"question": data.question}}

        # In a real impl, call LLM or use context. Here, mock with a brief delay.
        await asyncio.sleep(0)
        yield {"type": "tool.partial", "payload": {"draft": f"Answering: {data.question}"}}

        await asyncio.sleep(0)
        yield {
            "type": "tool.end",
            "payload": {
                "output": {"answer": "This is a placeholder answer.", "citations": []},
                "observation": {"summary": "Answered user question.", "artifacts": []},
            },
        }

