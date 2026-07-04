---
name: ui-evidence
description: Capture mandatory before/after screenshots (and videos/GIFs for flows) for any change touching UI/UX, and attach them to the PR. Use whenever a change touches frontend/pages, components, layouts, or assets.
---

# UI Evidence — screenshots are mandatory for UI/UX changes

Any PR touching `frontend/{pages,components,layouts,assets}/**` must show what
changed visually: before/after screenshots in the PR description, plus a
GIF/video when the change is a flow or animation. Only skip this for refactors
with zero visual effect — and say so explicitly in the PR description.

## Screenshots (always)

1. Boot + seed:
   ```bash
   tools/agent/boot_stack.sh
   cd backend && uv run python ../tools/agent/seed_org.py --demo
   export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers   # cloud sandboxes
   ```
2. Capture **before** (on the base branch or with your change stashed) and
   **after** — same page, same viewport:
   ```bash
   cd frontend
   node ../tools/agent/capture.mjs http://localhost:3000/<page> before.png
   # apply change / unstash, rebuild if prod mode:
   node ../tools/agent/capture.mjs http://localhost:3000/<page> after.png
   ```
   For authenticated pages, log in once with a throwaway Playwright script and
   `context.storageState({ path: 'state.json' })`, then pass `--state state.json`.
3. For RTL-sensitive work, capture `he` as well as `en` (set the locale via
   the picker in `/settings/general` or the `X-Locale` header).

## Videos / GIFs (for flows, animations, drag-drop, streaming UI)

Static shots can't show a flow. Record one:

```bash
node ../tools/agent/capture.mjs http://localhost:3000/<page> flow.webm --video 10
# or drive a multi-step flow in a throwaway spec with recordVideo, then:
ffmpeg -i flow.webm -vf "fps=10,scale=960:-1:flags=lanczos" flow.gif
```

GitHub renders GIFs inline; `.webm` cannot be attached via the API.

## Attaching evidence to the PR

Commit the files under `media/pr/<branch-slug>/` and reference them in the PR
body by their raw URL so they render inline:

```markdown
## UI changes
| Before | After |
|--------|-------|
| ![before](https://raw.githubusercontent.com/bagofwords1/bagofwords/<branch>/media/pr/<branch-slug>/before.png) | ![after](.../after.png) |

![flow](https://raw.githubusercontent.com/bagofwords1/bagofwords/<branch>/media/pr/<branch-slug>/flow.gif) 
```

Rules:
- Keep each image < 1 MB where possible (GIFs < 5 MB — trim fps/scale, not length).
- `media/pr/` files may be deleted after the PR merges; they are evidence, not assets.
- Never capture screens containing real credentials, tokens, or customer data —
  seeded sandbox data only.
- Humans attaching evidence may simply drag-and-drop into the PR description
  instead.
