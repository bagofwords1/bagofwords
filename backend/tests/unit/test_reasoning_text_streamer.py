"""Report reasoning stays attached to its tool across streaming and reload."""
import asyncio

import pytest

from app.ai.llm.types import ReasoningCompleteEvent, ReasoningDeltaEvent
from app.streaming.reasoning_streamer import ReasoningTextStreamer


@pytest.mark.asyncio
async def test_parallel_tool_summaries_preserve_prior_text_and_route_by_block():
    events, snapshots = [], {}
    async def emit(event):
        events.append(event)
    async def seq():
        return len(events) + 1
    def make(block_id, initial):
        async def persist(reasoning, content):
            snapshots[block_id] = reasoning
        return ReasoningTextStreamer(emit=emit, seq_fn=seq, completion_id='completion', agent_execution_id='execution', block_id=block_id, persist=persist, initial_reasoning=initial)
    left, right = make('left', 'Plan A.'), make('right', 'Plan B.')
    await asyncio.gather(left.append(ReasoningDeltaEvent(text='Code A.')), right.append(ReasoningDeltaEvent(text='Code B.')))
    await asyncio.gather(left.append(ReasoningCompleteEvent(text='Code A.')), right.append(ReasoningCompleteEvent(text='Code B.')))
    assert snapshots == {'left': 'Plan A.\n\nCode A.', 'right': 'Plan B.\n\nCode B.'}
    for block_id, expected in snapshots.items():
        texts = [e.data['text'] for e in events if e.event == 'block.delta.text' and e.data['block_id'] == block_id and e.data['field'] == 'reasoning']
        assert texts[-1] == expected
    # A retry / subsequent call appends a new segment, never repeats old text.
    await left.append(ReasoningCompleteEvent(text='Corrected A.'))
    assert snapshots['left'] == 'Plan A.\n\nCode A.\n\nCorrected A.'
    assert not any(e.event == 'block.delta.token' and e.data['field'] == 'content' for e in events)
