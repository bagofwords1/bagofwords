"""Append a tool's provider summaries to its report block's existing text."""
from app.ai.llm.types import ReasoningCompleteEvent, ReasoningDeltaEvent
from app.streaming.text_streamer import PlanningTextStreamer


class ReasoningTextStreamer(PlanningTextStreamer):
    def __init__(self, *, initial_reasoning: str = "", **kwargs):
        super().__init__(**kwargs)
        self.prev_reasoning = initial_reasoning
        self.summary = initial_reasoning
        self.segment = ""

    async def append(self, event):
        if isinstance(event, ReasoningDeltaEvent) and event.text:
            if not self.segment and self.summary:
                self.summary += "\n\n"
            self.segment += event.text
            self.summary += event.text
            await self.update(self.summary, "")
        elif isinstance(event, ReasoningCompleteEvent):
            # Some compatible providers return only a completed summary.
            if not self.segment and event.text:
                self.summary += ("\n\n" if self.summary else "") + event.text
                await self.update(self.summary, "")
            if self.summary:
                if self.persist:
                    await self.persist(self.summary, "")
                await self.complete()
            self.segment = ""
