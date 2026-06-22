"""Loop D — live LLM run delivering to the mock Teams outbox.

Configures a real Anthropic model for a seeded org, creates a catalog prompt,
subscribes with channel=teams, and runs the real scheduled path. Verifies a
genuine agent response is generated and delivered to the mock channel outbox.

Secrets via env only (never hard-coded):
    BOW_DATABASE_URL=sqlite:///db/app.db
    BOW_CHANNELS_MOCK=1
    ANTHROPIC_API_KEY=sk-ant-...
    BOW_ANTHROPIC_MODEL=claude-3-5-haiku-20241022   # optional override

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db BOW_CHANNELS_MOCK=1 \
      BOW_CHANNELS_MOCK_FILE=/tmp/loop_d_outbox.json \
      ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
      .venv/bin/python scripts/loop_d_live_anthropic.py
"""
import asyncio
import os
import sys
import uuid

import main  # noqa: F401  — imports routes/models, registering all ORM mappers
from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.llm_provider import LLMProvider
from app.models.llm_model import LLMModel
from app.models.scheduled_prompt import ScheduledPrompt
from app.services.prompt_catalog_service import prompt_catalog_service
from app.services.scheduled_prompt_service import scheduled_prompt_service
from app.services.channel_delivery_service import read_mock_outbox, clear_mock_outbox
from app.schemas.prompt_catalog_schema import PromptCatalogCreate


MODEL_ID = os.environ.get("BOW_ANTHROPIC_MODEL", "claude-3-5-haiku-20241022")


async def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("SKIP: ANTHROPIC_API_KEY not set")
        return 0
    if os.environ.get("BOW_CHANNELS_MOCK", "") not in ("1", "true", "yes"):
        print("SKIP: set BOW_CHANNELS_MOCK=1 to capture delivery")
        return 0

    clear_mock_outbox()
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"LoopD Org {suffix}")
        db.add(org)
        await db.flush()
        user = User(name=f"LoopD {suffix}", email=f"loopd-{suffix}@example.com",
                    hashed_password="x", is_active=True, is_verified=True)
        db.add(user)
        await db.flush()

        provider = LLMProvider(
            name="Anthropic (live)", provider_type="anthropic",
            organization_id=org.id, is_enabled=True, use_preset_credentials=False,
        )
        provider.encrypt_credentials(os.environ["ANTHROPIC_API_KEY"], "")
        db.add(provider)
        await db.flush()

        model = LLMModel(
            name="Claude Haiku (live)", model_id=MODEL_ID,
            provider_id=provider.id, organization_id=org.id,
            is_enabled=True, is_default=True, is_small_default=True,
            context_window_tokens=200000, max_output_tokens=2048,
        )
        db.add(model)
        await db.commit()

        prompt = await prompt_catalog_service.create_prompt(
            db,
            PromptCatalogCreate(
                title="Sanity Check",
                text="In one sentence, say hello and confirm you are operational.",
                scope="private", status="published", mode="chat", data_source_ids=[],
            ),
            current_user=user, organization=org,
        )
        from app.schemas.prompt_catalog_schema import SubscribeRequest
        sp = await prompt_catalog_service.subscribe(
            db, prompt.id,
            SubscribeRequest(cron_schedule="0 9 * * 1", channel="teams", run_mode="append"),
            current_user=user, organization=org,
        )
        sp_id = sp.id
        print(f"[setup] org={org.id} model={MODEL_ID} subscription={sp_id} channel=teams")

    # Run the real scheduled path (real LLM call inside create_completion).
    print("[run] invoking scheduled_run_prompt (live LLM)...")
    await scheduled_prompt_service.scheduled_run_prompt(sp_id)

    outbox = read_mock_outbox()
    teams = [e for e in outbox if e.get("channel") == "teams"]
    print(f"[result] teams deliveries: {len(teams)}")
    if teams:
        body = teams[0]["body"]
        print(f"[result] delivered body:\n---\n{body}\n---")
        ok = len(body.strip()) > 0
        print(f"[verdict] {'PASS' if ok else 'FAIL'} — live LLM response delivered to mock Teams")
        return 0 if ok else 1
    print("[verdict] FAIL — no teams delivery recorded")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
