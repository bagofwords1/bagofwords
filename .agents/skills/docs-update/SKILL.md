---
name: docs-update
description: Update the product docs at docs.bagofwords.com (Mintlify) with text and fresh screenshots after a user-facing change ships. Use when a merged change alters user-visible behavior, adds a feature, or when asked to update/refresh documentation.
---

# Docs Update — Mintlify + fresh screenshots

The product docs live on Mintlify (docs.bagofwords.com), managed through the
**Mintlify MCP server** — not in this repo. `docs/` here contains internal
design docs only.

## When to run

After a user-facing change merges: new feature, changed flow, renamed UI,
changed configuration. Skip for internal refactors.

## Flow

1. **Find what's affected.** With the Mintlify MCP: `checkout` the deployment
   (this opens an isolated editing session/branch — surface the returned
   `editorUrl` to the user), then `search` / `read` / `list_nodes` for pages
   mentioning the touched feature. List affected pages before editing.
2. **Capture fresh screenshots** of the new behavior from a seeded local stack
   (see the **ui-evidence** skill for the full capture procedure):
   ```bash
   tools/agent/boot_stack.sh && cd backend && uv run python ../tools/agent/seed_org.py --demo
   cd ../frontend && node ../tools/agent/capture.mjs http://localhost:3000/<page> shot.png
   ```
   Stage them under `docs/screenshots/pending-changes/<page-slug>/` in this
   repo so they're reviewable alongside the docs PR. Match the style of
   existing docs images (clean seeded data, 1440px wide, no dev toolbars).
3. **Edit pages** via the session tools:
   - body text → `edit_page` (string replace) or `write_page` (full rewrite)
   - frontmatter (title, description, icon) → `update_node`, never `edit_page`
   - new pages / navigation → `create_node`; site config → `update_config`
4. **Images**: if the MCP session cannot upload binary images, reference the
   staged files and note in the docs PR description that the images in
   `docs/screenshots/pending-changes/<page-slug>/` must be uploaded via the
   Mintlify editor (`editorUrl`) before merge. Do not publish pages pointing
   at broken image paths.
5. **Review the diff** (`diff` / `get_session_state`), then `save` — this opens
   a docs PR. **Never** use Mintlify code-mode (`execute_code`) for content
   work: it writes straight to the live deployment with no PR safety net.
6. Report back: affected pages, the docs PR link, and the `editorUrl`.

## Writing rules

- Describe what the user sees now — don't narrate the change ("previously…",
  "as of this release…") unless editing a changelog page.
- Verify every claim against the running app you just booted, not against the
  code diff — docs describe behavior, and this catches half-shipped UI.
- Screenshots must come from seeded sandbox data only — never real customer
  names, tokens, or connection strings.
- Keep terminology consistent with the app's locale catalogs
  (`locales/en.json`) — the UI string is the source of truth for feature names.
