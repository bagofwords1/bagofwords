"""Drive a catalog model through BOW and verify its persisted standard-tier cost."""
import asyncio
import json

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.ai.llm.llm import LLM, _PENDING_RECORD_TASKS
from app.ai.llm.types import Message
from app.dependencies import async_session_maker
from app.models.llm_model import LLMModel
from app.models.llm_usage_record import LLMUsageRecord


@pytest.mark.parametrize("model_id,input_rate,output_rate", [
    ("gpt-6.2-sol", 2, 10),
    ("gpt-6.2-astra", 8, 40),
])
@pytest.mark.parametrize("input_tokens,input_factor,output_factor", [
    (272_000, 1, 1),
    (272_001, 2, 1.5),
])
def test_cache_and_context_rates_reach_usage_record(
    test_client, create_user, login_user, whoami, model_id, input_rate, output_rate,
    input_tokens, input_factor, output_factor,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}
    response = test_client.post("/api/llm/providers", headers=headers, json={
        "name": "OpenAI", "provider_type": "openai", "credentials": {"api_key": "test"},
        "models": [{"name": model_id, "model_id": model_id, "is_custom": False}],
    })
    assert response.status_code == 200, response.text

    def respond(http_request):
        assert http_request.url.path == "/v1/responses"
        request = json.loads(http_request.content)
        assert request["model"] == model_id
        assert request["input"][0] == {"role": "developer", "content": "Stable instructions"}
        assert request["reasoning"]["effort"] == "low"
        event = {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {
                "input_tokens": input_tokens, "output_tokens": 900,
                "input_tokens_details": {
                    "cached_tokens": 100_000, "cache_write_tokens": 40_000,
                    "cache_write_tokens_details": {"ttl_30m_tokens": 30_000, "ttl_12h_tokens": 10_000},
                },
            }},
        }
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              text=f"data: {json.dumps(event)}\n\n")

    async def run():
        async with async_session_maker() as db:
            model = (await db.scalars(select(LLMModel).options(selectinload(LLMModel.provider)).where(
                LLMModel.model_id == model_id, LLMModel.organization_id == org_id,
            ))).one()
            llm = LLM(model, usage_session_maker=async_session_maker)
            original = llm.client.async_client
            http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            llm.client.async_client = original.with_options(http_client=http_client)
            try:
                async for _ in llm.inference_stream_v2(
                    messages=[Message(role="user", content="Hi")], system="Stable instructions",
                    usage_scope="test.gpt62", thinking={"type": "disabled"},
                ):
                    pass
                await asyncio.gather(*_PENDING_RECORD_TASKS)
            finally:
                await llm.client.async_client.close()
                await original.close()
                llm.client.client.close()
            record = (await db.scalars(select(LLMUsageRecord).where(
                LLMUsageRecord.llm_model_id == model.id,
            ))).one()
            assert record.cache_creation_tokens == 40_000
            assert record.cache_read_tokens == 100_000
            weighted_input = input_tokens - 140_000 + 100_000 * 0.05 + 30_000 * 1.25 + 10_000 * 2.5
            assert float(record.input_cost_usd) == pytest.approx(weighted_input * input_rate * input_factor / 1e6)
            assert float(record.output_cost_usd) == pytest.approx(900 * output_rate * output_factor / 1e6)

    asyncio.run(run())
