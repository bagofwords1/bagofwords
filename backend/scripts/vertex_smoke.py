"""Live smoke test for the Vertex provider, through the real LLM facade.

Builds an in-memory LLMProvider/LLMModel pair (no DB), then drives
LLM.inference_stream_v2 for one model per Vertex surface, so the whole
path — credential resolution, transport selection, endpoint derivation,
streaming, usage extraction — is exercised exactly as a report would.

Usage:
    uv run python scripts/vertex_smoke.py <service-account-key.json> [project_id]
"""

import asyncio
import json
import sys

from cryptography.fernet import Fernet

import main  # noqa: F401  — registers every SQLAlchemy mapper (uvicorn only runs under __main__)
from app.settings.config import settings
from app.models.llm_provider import LLMProvider
from app.models.llm_model import LLMModel
from app.ai.llm.llm import LLM
from app.ai.llm.types import Message


def build(model_id: str, key_json: str, project_id: str, location: str = "global") -> LLMModel:
    provider = LLMProvider(
        name="Vertex",
        provider_type="vertex",
        organization_id="org-smoke",
        additional_config={
            "project_id": project_id,
            "location": location,
            "auth_mode": "service_account",
        },
    )
    fernet = Fernet(settings.bow_config.encryption_key)
    provider.api_key = fernet.encrypt(json.dumps(key_json).encode()).decode()
    provider.api_secret = fernet.encrypt(json.dumps(None).encode()).decode()
    return LLMModel(
        name=model_id,
        model_id=model_id,
        provider=provider,
        organization_id="org-smoke",
        is_enabled=True,
    )


async def probe(model_id: str, key_json: str, project_id: str) -> None:
    model = build(model_id, key_json, project_id)
    llm = LLM(model)
    client = type(llm.client).__name__
    base = getattr(getattr(llm.client, "async_client", None), "base_url", None)
    print(f"\n=== {model_id}")
    print(f"    client   : {client}")
    print(f"    base_url : {base}")
    text, tools, usage = "", [], None
    try:
        async for ev in llm.inference_stream_v2(
            messages=[Message(role="user", content="Reply with exactly: vertex ok")],
            system="You are terse.",
            tools=[],
        ):
            kind = type(ev).__name__
            if kind == "TextDeltaEvent":
                text += ev.text
            elif kind == "ToolUseCompleteEvent":
                tools.append(ev.name)
            elif kind == "UsageEvent":
                usage = ev
        print(f"    text     : {text.strip()[:80]!r}")
        print(f"    usage    : in={getattr(usage, 'input_tokens', None)} "
              f"out={getattr(usage, 'output_tokens', None)} "
              f"cache_read={getattr(usage, 'cache_read_tokens', None)}")
        print("    RESULT   : OK")
    except Exception as exc:
        msg = str(exc).replace("\n", " ")
        print(f"    RESULT   : {type(exc).__name__}: {msg[:220]}")


async def main() -> None:
    key_path = sys.argv[1]
    key_json = open(key_path).read()
    project_id = sys.argv[2] if len(sys.argv) > 2 else json.loads(key_json)["project_id"]
    print(f"project: {project_id}")
    for model_id in ("gemini-3.8-flash", "gemini-3.1-pro-preview", "claude-sonnet-5", "zai-org/glm-5.2-maas"):
        await probe(model_id, key_json, project_id)


if __name__ == "__main__":
    asyncio.run(main())
