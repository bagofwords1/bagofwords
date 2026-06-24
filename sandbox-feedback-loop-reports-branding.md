# Sandbox Feedback Loop — Report avatar: company branding + per-model overlay (+ Claude logo)

Validates two related frontend changes, end-to-end, in a fresh cloud sandbox:

1. **Anthropic → Claude logo.** The provider icon shipped for `anthropic` is
   replaced with the orange **Claude** mark (vendored from the worldvectorlogo
   SVG, rasterized locally — no CDN hotlink). Both the square badge
   (`anthropic-icon.png`, used by compact `:icon` slots) and the wide wordmark
   (`anthropic.png`, used in "powered by" / provider-card slots) are updated.

2. **Report avatar = company brand image + model overlay.** On
   `frontend/pages/reports/[id]/index.vue` the assistant avatar no longer
   hardcodes the BoW logo. It now renders the **organization's uploaded brand
   image** (`config.general.icon_url`, falling back to the BoW logo when none is
   set), **height-bound with a capped max-width** so logos of any aspect ratio
   render cleanly, and overlays a **small badge for the LLM brand** that produced
   that specific completion. A `UTooltip` on the avatar shows which model was
   used (`Generated with <model>`).

   Brand resolution is **name-first** (`resolveModelBrand`,
   `frontend/utils/llmBrand.ts`): the model id/name is matched first
   (`claude→anthropic`, `gpt/o1/o3→openai`, `gemini→google`, …) and only falls
   back to the hosting provider type. So a Claude/GPT model served via **AWS
   Bedrock** or a **custom OpenAI-compatible endpoint** still shows its true
   model brand, not the host. Unknown names fall back to a generic chip.

---

## What changed

| File | Change |
|---|---|
| `frontend/public/llm_providers_icons/anthropic-icon.png` | Claude burst, 300×300 (square badge) |
| `frontend/public/llm_providers_icons/anthropic.png` | Claude burst + "Claude" wordmark, 2560×288 |
| `frontend/utils/llmBrand.ts` | New `resolveModelBrand(model, providerType?)` — name-first brand resolver |
| `frontend/pages/reports/[id]/index.vue` | Avatar: org `icon_url` (fallback BoW), height-bound + `max-w-[72px]`, model overlay badge, `UTooltip` with model name; thread `model` through `ChatMessage` |

The completion's model is already available to the frontend — `CompletionV2Schema.model`
(`backend/app/schemas/completion_v2_schema.py`) — so no backend change was needed
to know which model produced each message.

---

## Environment setup (fresh sandbox)

Per `docs/design/sandbox-feedback-loop.md`. Backend on Python 3.12, local auth
enabled for UI login, mock data seeded directly into SQLite (no LLM key needed —
we only need persisted completions with varied `model` values to drive the overlay).

```bash
# Backend
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install uv && uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" BOW_SMTP_PASSWORD="dummy"
export BOW_CONFIG_PATH="$PWD/../configs/bow-config.dev.yaml"
mkdir -p db uploads/files uploads/branding
alembic upgrade head
python main.py &        # auto-reloads

# configs/bow-config.dev.yaml: features.allow_uninvited_signups: true, auth.mode: "local_only"

# Frontend
cd ../frontend && yarn install && yarn dev &
npx playwright install chromium
```

### Seed: report + completions across 4 model brands

Register `sandbox@bow.dev`, create a report, then insert one assistant
completion per brand directly into `db/app.db` (see the script used in this
session): models `gpt-5.4` (openai), `claude-sonnet-4-6` (anthropic),
`gemini-3-pro-preview` (google), and `mistral-large-latest` (unknown → generic
chip). Upload a company brand image via `POST /api/organization/general/icon`
(form field `icon`).

---

## Loop — visual validation (Playwright + Claude's eyes)

```bash
RID=<report_id> OUT=/tmp/shots LABEL=wide node scratchpad/shot.mjs
```

The script logs in through the local-auth form, opens `/reports/<id>`,
screenshots the full thread, then hovers an assistant avatar to capture the
model tooltip. To validate the "any aspect ratio" claim, re-upload the org icon
as a wide / tall / square image and re-screenshot.

### Observed

**Backend / data path (live):** registered `sandbox@bow.dev`, created a report,
seeded 4 assistant completions, uploaded a company brand image. The v2 endpoint
`GET /api/reports/<id>/completions` returns each completion's `model` and block
content as expected:

```
system | model=gpt-5.4              | blocks=1 | "Here's your **monthly revenue**..."
system | model=claude-sonnet-4-6    | blocks=1 | "Broken down by **category**..."
system | model=gemini-3-pro-preview | blocks=1 | "**APAC** is your fastest-growing..."
system | model=mistral-large-latest | blocks=1 | "Based on the trend, **Q1 next year**..."
```

**Avatar render (Chromium):** the assistant avatar markup was rendered with the
pre-installed Chromium (`/opt/pw-browsers`), using the exact Tailwind class
semantics from the `.vue` change, the real `/llm_providers_icons/*-icon.png`
assets, and the real `resolveModelBrand` mapping. Two shots:

- `harness_thread.png` — company brand (`ACME`) avatar per message, overlay
  correctly resolved per model: `gpt-5.4 → openai`, `claude-sonnet-4-6 →
  anthropic` (the new **Claude burst**), `gemini-3-pro-preview → google`,
  `mistral-large-latest → custom` (generic chip). Tooltip reads
  **"Generated with claude-sonnet-4-6"**.
- `harness_dimensions.png` — the same avatar with the org logo as **wide
  (480×120)**, **tall (120×320)**, **square (256×256)**, and **no org icon →
  BoW fallback**. All render height-bound (28px), capped at `max-w-[72px]`,
  `object-contain` — no distortion or overflow at any aspect ratio.

> Note: the live `yarn dev` could not be exercised in this sandbox — the
> frontend `npm`/`yarn` install would not complete on the environment's network
> (optional platform esbuild binaries, e.g. `@esbuild/win32-x64`,
> `@esbuild/darwin-arm64`, abort repeatedly; the full tree is ~1GB). The
> backend, DB, API and asset paths were validated live; the avatar was validated
> by faithfully rendering its markup. On a network that can complete the install,
> `scratchpad/shot.mjs` drives the real page (login → `/reports/<id>` → full-page
> + tooltip screenshots).

---

## What this proves

- The `anthropic` provider icon is now the Claude mark everywhere
  `LLMProviderIcon` renders it (the report overlay uses the square `-icon`
  variant directly).
- Each assistant message is branded with the company image and tagged with the
  exact LLM brand that produced it, resolved name-first so Bedrock/custom-hosted
  models still show correctly.
- Company logos of any aspect ratio render cleanly because the avatar is bound
  by height with a capped max-width.
