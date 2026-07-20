# Feedback Loop — "support image generation for LLM models (OpenAI) and embed generated images/PDFs in artifacts"

Add image generation (OpenAI `gpt-image-1`) as a first-class capability: a
`generate_image` agent tool produces an image, stores it as a `File`, and
`create_artifact` / `edit_artifact` embed it (and uploaded images/PDFs) in a
dashboard via a new `<BowFile>` sandbox component. This loop validates the whole
path with a mock image API (Loop A, no credentials) and against the real OpenAI
API (Loop B, real key).

## Root cause / gap (validated)

Before this change the LLM stack was text-out only. Evidence:
- `LLMClient` (`backend/app/ai/llm/clients/base.py`) exposed only `inference*`;
  the only multimodal support was image *input* (`ImageInput` + `supports_vision`).
- No model in `LLM_MODEL_DETAILS` (`backend/app/models/llm_model.py`) was an
  image-generation model; no capability flag existed.
- Artifacts (`create_artifact`) render React in a sandboxed `srcdoc` iframe whose
  charts are `<EChart>` canvases — there was no path to display a stored image or
  PDF file inside an artifact. Same-origin `/files/{id}/content` is auth-gated, so
  a bare `<img src>` inside the iframe 401s.

## The change

Backend:
- `ImageOutput` type (`types.py`); `generate_image()` on `LLMClient` (base raises
  NotImplementedError) implemented on both OpenAI clients (`openai_client.py`,
  `openai_responses_client.py`) via the Images API; facade `LLM.generate_image`
  gated by `supports_image_generation` (`llm.py`).
- `supports_image_generation` column (migration `img1gen2col3`) + catalog entry
  `gpt-image-1`, resolved on sync (`llm_service.py`).
- `generate_image` tool (`tools/implementations/generate_image.py`) → stores the
  PNG via `FileService.save_bytes_as_file`, returns `file_id`.
- `create_artifact` / `edit_artifact` accept `file_ids`, store `content.files`
  (`{id, content_type, filename}`), and instruct the model to use `<BowFile>`.

Frontend:
- `<BowFile>` sandbox global (`public/libs/artifact-globals.js`): renders images
  inline, PDFs via `<object>` (with an "Open PDF" fallback card for sandboxes that
  block the plugin), plus an annotation overlay via children.
- `ArtifactFrame.vue` fetches embedded files with auth and injects them into
  `ARTIFACT_DATA.files` as data URIs (no auth needed inside the iframe).

## Loop A — deterministic reproduction (no external services)

Mock OpenAI-compatible image + codegen server, then the full tool path:

```bash
cd backend
# 1. Mock image API (images.generations returns a real PNG; chat returns <BowFile> code)
MOCK_IMAGE_PORT=9098 uv run python ../tools/agent/mock_image_llm.py &   # setsid in practice
# 2. Boot stack + seed an org (see tools/agent/boot_stack.sh, seed_org.py)
# 3. Build the demo: generate_image -> File -> create_artifact(file_ids) -> Artifact
TESTING=true ENVIRONMENT=production TEST_DATABASE_URL="sqlite:///db/agent.db" \
  MOCK_URL="http://127.0.0.1:9098/v1" uv run python ../tools/agent/img_demo_build.py
# prints report_id / file_id / pdf_file_id / artifact_id (artifact_error: null)
```

Regression test (survives as `tests/unit/test_image_generation.py`):

```bash
uv run pytest tests/unit/test_image_generation.py -q     # 9 passed
```

Observed PASS: `generate_image` stores a File; `create_artifact` stores
`content.files` and BowFile code; the artifact renders the image (with the
annotation overlay) and the PDF card in the real UI (Playwright screenshots).

## Loop B — live confirmation (real credentials)

Real `gpt-image-1` via our client, key from env only (never committed/logged):

```bash
OPENAI_API_KEY=sk-... uv run python <scratch>/live_openai_image.py   # 2 prompts -> 2 PNGs
# and end-to-end in the UI (real image, mock only for codegen):
REAL_OPENAI_KEY=sk-... MOCK_URL=http://127.0.0.1:9098/v1 \
  uv run python ../tools/agent/img_demo_build.py
```

Observed: real PNGs returned (~1.1–1.6 MB) with image-token usage; the generated
image renders inside the artifact via `<BowFile>`.

## What this proves / notes

- End-to-end: prompt → OpenAI image → `File` → artifact embed → sandbox render.
- **Inline PDF viewer:** PDFs render inline via pdf.js (pages → canvas) inside the
  sandboxed iframe, where the native PDF plugin is blocked. `<BowFile>` falls back
  to an "Open PDF" card only when pdf.js is unavailable (e.g. the headless
  thumbnail render). pdf.js is vendored via `scripts/download-vendor-libs.sh`.
- **Chat picker:** image-generation models (`supports_image_generation`) are
  excluded from the chat/agent model pickers (ModelSelector, PromptBoxV2,
  TestPromptBox), and can never be the org default or small default (guarded in
  `llm_service` sync + the set-default endpoint; catalog sets both flags False).
- Secrets: env vars only; the demo scripts read keys from the environment.
