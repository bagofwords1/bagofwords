#!/usr/bin/env python3
"""Build an end-to-end image-generation demo against the running DB.

Exercises the REAL feature path with the mock image server (no real key):
  generate_image tool -> File  ->  create_artifact tool (file_ids) -> Artifact

Prereqs: backend running on db/agent.db, org seeded (seed_org.py), and the mock
image server on MOCK_URL. Prints report_id / artifact_id / file_id as JSON.

Run:  cd backend && uv run python ../tools/agent/img_demo_build.py
"""
import asyncio
import json
import os
import uuid

# Import the app module (sets up routes/services/models exactly as the running
# server does, so the SQLAlchemy mapper registry is the same consistent set).
# Does NOT boot uvicorn (that's guarded by __main__ in main.py).
import main  # noqa: F401

from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.report import Report
from app.models.llm_provider import LLMProvider
from app.models.llm_model import LLMModel
from app.ai.registry import ToolRegistry

MOCK_URL = os.environ.get("MOCK_URL", "http://127.0.0.1:9098/v1")
ORG_NAME = os.environ.get("ORG_NAME", "Agent Org")


async def _drain(tool, tool_input, runtime_ctx):
    last_end = None
    async for evt in tool.run_stream(tool_input, runtime_ctx):
        etype = getattr(evt, "type", "")
        if etype == "tool.end":
            last_end = evt.payload
    return last_end


async def main():
    registry = ToolRegistry()
    async with async_session_maker() as db:
        org = (await db.execute(select(Organization).limit(1))).scalars().first()
        assert org, "no organization found — run seed_org.py first"
        user = (await db.execute(select(User).where(User.id == None))).scalars().first()  # noqa: E711
        if user is None:
            user = (await db.execute(select(User).limit(1))).scalars().first()
        assert user, "no user found"

        # 1. Provider -> mock (openai type, base_url routes to the mock server)
        prov = (await db.execute(
            select(LLMProvider).where(
                LLMProvider.organization_id == org.id, LLMProvider.provider_type == "openai"
            )
        )).scalars().first()
        if prov is None:
            prov = LLMProvider(
                name="OpenAI (mock)", provider_type="openai",
                organization_id=org.id, is_enabled=True, use_preset_credentials=False,
            )
            db.add(prov)
        prov.additional_config = {"base_url": MOCK_URL}
        prov.encrypt_credentials("mock-key", "")
        await db.commit()
        await db.refresh(prov)

        # 2. Models: a chat model (codegen) + the image-gen model
        async def ensure_model(model_id, name, **flags):
            m = (await db.execute(
                select(LLMModel).where(
                    LLMModel.organization_id == org.id,
                    LLMModel.provider_id == prov.id,
                    LLMModel.model_id == model_id,
                )
            )).scalars().first()
            if m is None:
                m = LLMModel(
                    name=name, model_id=model_id, provider_id=prov.id,
                    organization_id=org.id, is_preset=False,
                )
                db.add(m)
            for k, v in flags.items():
                setattr(m, k, v)
            return m

        chat = await ensure_model(
            "gpt-5.6-luna", "GPT-5.6 Luna (mock)",
            is_enabled=True, is_default=True, is_small_default=True,
            supports_vision=False,  # skip the headless preview screenshot path
            supports_image_generation=False, context_window_tokens=1050000,
            max_output_tokens=128000,
        )
        await db.commit()
        await db.refresh(chat)

        # Image model: on the mock provider by default, or on a REAL OpenAI
        # provider (no base_url) when REAL_OPENAI_KEY is set — so we can render a
        # genuine gpt-image-1 image in the UI while codegen stays on the mock.
        real_key = os.environ.get("REAL_OPENAI_KEY")
        if real_key:
            real_prov = (await db.execute(
                select(LLMProvider).where(
                    LLMProvider.organization_id == org.id, LLMProvider.name == "OpenAI (real)"
                )
            )).scalars().first()
            if real_prov is None:
                real_prov = LLMProvider(
                    name="OpenAI (real)", provider_type="openai",
                    organization_id=org.id, is_enabled=True, use_preset_credentials=False,
                )
                db.add(real_prov)
            real_prov.additional_config = {}  # no base_url -> real api.openai.com
            real_prov.encrypt_credentials(real_key, "")
            await db.commit()
            await db.refresh(real_prov)
            img_provider_id = real_prov.id
        else:
            img_provider_id = prov.id

        img_model = (await db.execute(
            select(LLMModel).where(
                LLMModel.organization_id == org.id,
                LLMModel.provider_id == img_provider_id,
                LLMModel.model_id == "gpt-image-1",
            )
        )).scalars().first()
        if img_model is None:
            img_model = LLMModel(
                name="GPT Image 1", model_id="gpt-image-1", provider_id=img_provider_id,
                organization_id=org.id, is_preset=False,
            )
            db.add(img_model)
        img_model.supports_image_generation = True
        img_model.supports_vision = False
        await db.flush()
        # Enable ONLY the chosen image model (on img_provider_id); disable any
        # other image model so the tool deterministically picks the right one.
        all_imgs = (await db.execute(
            select(LLMModel).where(
                LLMModel.organization_id == org.id,
                LLMModel.supports_image_generation == True,  # noqa: E712
            )
        )).scalars().all()
        for m in all_imgs:
            m.is_enabled = (m.provider_id == img_provider_id and m.model_id == "gpt-image-1")
        await db.commit()

        # 3. Report
        report = Report(
            title="Image Generation Demo",
            slug=f"img-demo-{uuid.uuid4().hex[:8]}",
            status="draft",
            user_id=user.id,
            organization_id=org.id,
        )
        db.add(report)
        await db.commit()
        await db.refresh(report)

        base_ctx = {"db": db, "organization": org, "user": user, "report": report}

        # 4. generate_image tool -> File
        gi = registry.get("generate_image")
        gi_end = await _drain(
            gi,
            {"prompt": "a friendly teal robot mascot holding a bar chart, flat vector style",
             "title": "Robot Mascot"},
            base_ctx,
        )
        gi_obs = (gi_end or {}).get("observation", {})
        file_id = gi_obs.get("file_id")
        assert file_id, f"generate_image failed: {gi_obs}"

        # 4b. Also create a small PDF file to prove the <BowFile> PDF viewer path.
        import io
        from PIL import Image, ImageDraw
        from app.services.file_service import FileService
        pdf_img = Image.new("RGB", (816, 1056), "white")
        d = ImageDraw.Draw(pdf_img)
        d.rectangle([0, 0, 816, 90], fill="#4f46e5")
        d.text((40, 34), "Quarterly Report — Generated Asset (PDF)", fill="#ffffff")
        for i, line in enumerate([
            "This PDF is embedded in a BOW artifact via <BowFile>.",
            "It renders in an in-sandbox PDF viewer (no auth/URL handling).",
            "Annotations can be overlaid on top, same as images.",
        ]):
            d.text((40, 150 + i * 40), line, fill="#0f172a")
        d.rectangle([40, 320, 776, 700], outline="#94a3b8", width=2)
        d.text((60, 340), "[ chart placeholder ]", fill="#64748b")
        pdf_buf = io.BytesIO()
        pdf_img.save(pdf_buf, format="PDF")
        pdf_file = await FileService().save_bytes_as_file(
            db=db, content=pdf_buf.getvalue(), filename="quarterly-report.pdf",
            content_type="application/pdf", current_user=user, organization=org,
            report_id=str(report.id),
        )
        pdf_file_id = str(pdf_file.id)

        # 5. create_artifact tool (file-only artifact) -> Artifact with BowFile code
        ca = registry.get("create_artifact")
        ca_ctx = dict(base_ctx, model=chat)
        ca_end = await _drain(
            ca,
            {"prompt": "A clean page showing the generated mascot image with a title.",
             "title": "Generated Image Gallery",
             "mode": "page",
             "visualization_ids": [],
             "file_ids": [file_id, pdf_file_id]},
            ca_ctx,
        )
        ca_out = (ca_end or {}).get("output", {})
        artifact_id = ca_out.get("artifact_id")

        print(json.dumps({
            "report_id": str(report.id),
            "report_slug": report.slug,
            "file_id": file_id,
            "pdf_file_id": pdf_file_id,
            "artifact_id": artifact_id,
            "artifact_error": None if artifact_id else ca_out,
        }, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
