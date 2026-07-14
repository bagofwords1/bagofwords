---
name: readme-showcase
description: Build or refresh a product README showcase using a seeded Bag of Words workspace, polished in-product screenshots, and repository-ready visual assets. Use when positioning has changed, README screenshots are stale, a launch/demo workspace is needed, or the user asks to populate agents, reports, dashboards, evals, automations, connectors, or monitoring for product storytelling.
---

# README Showcase

Create a credible demo workspace first, then capture the product as it actually
works. Treat the README copy, seeded state, and screenshots as one deliverable.

## Ground Rules

- Read `.agents/skills/ui-evidence/SKILL.md` before capturing product screens.
- Use only seeded, synthetic, sample, or explicitly approved demo data. Never
  capture customer data, credentials, tokens, or raw API keys.
- If an external LLM will receive local data, state that clearly and obtain
  explicit approval before running the analysis. A pasted API key is not a
  substitute for informed data-sharing approval.
- Give demo users plausible names and roles. Avoid labels such as `admin`,
  `test`, or `demo user` in final screenshots.
- Preserve the product UI pixels. Generate backgrounds separately and compose
  screenshots deterministically; never ask an image model to redraw UI text.

## Workflow

### 1. Define the Story

Read the current README and list the product claims each screenshot must prove.
For the current product direction, cover:

- Bring any LLM and connect any data.
- Agentic analytics: reports, dashboards, query generation, deep analysis, and
  root-cause analysis.
- Data agents with scoped data, tools, credentials, instructions, permissions,
  starters, and channels.
- Automations, schedules, and event-driven triggers.
- Evals, LLM-as-judge checks, and self-improving instruction loops.
- Full run observability: traces, plans, tool calls, costs, latency, feedback,
  and reliability diagnosis.
- MCP gateway, connectors, RBAC, and enterprise governance.

Write the desired screenshot list before seeding. Prefer eight strong scenes:

1. Executive dashboard open beside its conversation.
2. Root-cause analysis conversation.
3. Agents overview showing several distinct domains.
4. Pending instruction change or build review.
5. Eval-driven self-learning settings.
6. Connector catalog.
7. Automation trigger or scheduled task.
8. Monitoring with traces and judge/reliability signals.

### 2. Build the Demo Workspace

Inspect available sample databases before inventing data. Connect two to four
varied foundations, then create domain agents that reflect real ownership
boundaries rather than duplicate assistants. A strong enterprise set includes
System Logs, Sales, Finance, Procurement, Workforce, Executive Operations, and
Customer Analytics.

For every agent:

- Select only the relevant connections and tables.
- Add a concise description, context, operating instruction, and four starters.
- Configure channels and an appropriate production/training status.
- Use distinct icons and clean names.
- Add eval cases that test calculations, evidence, tool use, and decision quality.

Seed enough state to make every target screen real: reports, one polished page
artifact, an RCA, an eval suite, a pending build, self-learning policy, a
trigger or schedule, and monitoring traces/results. Prefer product APIs over
direct database writes. Use local deterministic seeding only when external
inference is not approved or a repeatable fixture is required, and disclose it.

### 3. Capture Clean Product States

Use the in-app browser or `tools/agent/capture.mjs`. Keep one signed-in demo
session and a consistent viewport. Capture at `1280x720` unless the existing
README uses another stable standard. With the in-app browser, use screenshot
scale `css` to avoid device-scale artifacts.

Before each capture:

- Close unrelated menus and transient notifications.
- Ensure titles, labels, and table contents are readable.
- Check that no text is clipped and no fixed element overlaps the focal area.
- Keep the active navigation item visible where it adds context.
- Verify the screen contains no secret, email beyond the demo identity, or
  unapproved local data.

Inspect every raw screenshot at original resolution before composition.

### 4. Add Painted Backdrops

Use image generation for two or three coordinated, wide natural or abstract
paintings. Ask for a quiet low-contrast center, richer edge detail, off-white
paper texture, and a restrained multi-color palette. Exclude text, logos,
gradient shapes, bokeh, and UI elements.

Place each untouched product screenshot over a backdrop with:

- A fixed 16:9 final canvas, normally `1280x720`.
- Roughly 8-10% breathing room around the screenshot.
- A subtle white edge, 6-8px corner radius, and restrained shadow.
- No labels or decorative UI outside the product screenshot.

Store final assets under `media/readme/final/`. Keep each PNG below 1 MB where
possible. Keep raw captures and generated backgrounds outside the repo unless
the user explicitly asks for source assets.

### 5. Update the README

Keep the writing direct and product-specific. Lead with the product and the
literal value, not category jargon. Preserve useful integration tables and
replace stale images in place.

Recommended narrative order:

1. Positioning and executive dashboard.
2. Chat, deep analysis, and RCA.
3. Agent context and agents-at-scale management.
4. Automations and channels.
5. Evals, self-learning, and monitoring.
6. Architecture, LLM providers, data connectors, MCP, and enterprise controls.

Use descriptive alt text and repository-relative image paths. Do not leave the
same screenshot in multiple sections unless it is intentionally the hero.

## Verification

- Run `git diff --check`.
- Confirm every README image path exists.
- Confirm final dimensions are consistent.
- Visually inspect every final image at original resolution.
- Check `git status --short --untracked-files=all` and keep only intended README
  and `media/readme/final/` changes.
- Leave the local app open on the strongest showcase screen and report the URL.
- Tell the user when external LLM runs were replaced with local deterministic
  demo content, and recommend rotating any key pasted into chat.
