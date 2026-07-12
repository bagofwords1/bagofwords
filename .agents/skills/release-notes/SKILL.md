---
name: release-notes
description: Bump the version and write a CHANGELOG.md release-notes entry after a user-facing change ships. Use when asked to "bump the version", "cut a release", "add a changelog entry", or "write release notes". Produces one minimal, user-facing, technical bullet per change.
---

# Release notes & version bump

Two files, one commit. `VERSION` drives the release; `CHANGELOG.md` is the
human-readable history.

- **`VERSION`** — a single line, `MAJOR.MINOR.PATCH` (e.g. `0.0.448`). The
  Release workflow (`.github/workflows/release.yml`) reads it, tags `vX.Y.Z`,
  and extracts the matching `CHANGELOG.md` section as the GitHub release body.
- **`CHANGELOG.md`** — newest version first. Served to users at `/changelog`
  (parsed by `backend/app/routes/changelog.py`), so the format is load-bearing.

## Steps

1. **Bump `VERSION`.** Increment the **patch** by one (`0.0.448` → `0.0.449`)
   unless told otherwise. No leading spaces, no trailing newline changes —
   just the number.
2. **Add a section at the top of `CHANGELOG.md`**, directly under
   `# Release Notes`:
   ```
   ## Version <new-version> (<Month DD, YYYY>)
   - **<Title> (#<PR>)** — <one-line description>.
   ```
   Use today's date, format `July 12, 2026`. One bullet per user-facing
   change; group the whole release under this single version heading.
3. **Commit both files together**: `Bump version to <X.Y.Z> with changelog entry`.

## How to write a bullet

Format: `- **Title (#PR)** — description.`

- **Bold title** — a short noun phrase for the feature/fix (`**Elasticsearch
  connector**`, `**Fix SSO login for mismatched email casing**`). Prefix fixes
  with `Fix`.
- **`(#PR)`** — the pull request number, if known. Omit the parens entirely if
  there's no PR.
- **` — ` (em-dash + spaces)** separates title from description.
- **Description** — what the user can now do, or what stopped being broken.
  Technical but plain. Name the real thing (`PromQL`, `SSE`, `.xlsx`, the org
  setting, the env var). One sentence. End with a period.

### Rules

- **Minimal.** One line per change. If it needs two sentences, the second adds
  a concrete knob (default value, env var, setting name) — not narration.
- **User-facing only.** Skip pure refactors, test-only changes, CI, and
  dependency bumps. If a user can't observe it, it's not a release note.
- **Present tense, user's point of view.** "Charts export to Excel" — not
  "Added the ability to export" or "We changed the exporter".
- **No filler.** Drop "now", "the ability to", "support for", "improved",
  "various", "enhancements". State the capability directly.
- **Be specific, not vague.** "caps requests per minute/hour/day, enforced as
  a hard block" beats "better rate limiting".

## Examples

Feature:
```
- **Prometheus connector (#595)** — query metrics with PromQL over the Prometheus HTTP API; each metric becomes a table.
```

Feature with a knob:
```
- **Concurrent multi-tool execution (#598)** — one planner decision can run its tool calls in parallel, controlled by the `ai_tool_concurrency` org setting (defaults to 4; set to 1 for serial).
```

Fix:
```
- **Fix iOS focus-zoom on the report prompt box (#600)** — the mobile prompt field is pinned to 16px so tapping it no longer zooms the viewport.
```

Fix with cause:
```
- **Fix profile Usage tab never updating (#576)** — usage counters now record without a hard cap configured, and the tab refreshes the session on open instead of showing stale zeros.
```

Config/model change (no PR):
```
- **OpenAI model presets** — add GPT-5.6 Sol, Terra, and Luna; make Terra the default and retire the GPT-5.4/5.2 presets.
```

A full release section:
```
## Version 0.0.438 (July 7, 2026)
- **Triggers (#562)** — user-owned webhooks that spawn agent sessions, plus report-per-run routing for scheduled tasks, under a new Automations page.
- **QVD indexing progress (#564)** — real per-file indexing progress with stop, file size, and duration.
- **WhatsApp fixes (#565)** — agent replies (text + data) are delivered back to WhatsApp, and the verification page shows WhatsApp branding instead of Slack.
```
