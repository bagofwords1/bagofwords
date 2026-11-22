# Mintlify drafts: agent routing with folders

Drafts for docs.bow.ai, staged here because the Mintlify MCP was not connected
in the session that wrote them. Publish through the **docs-update** skill
(Mintlify `checkout` → `create_node` / `write_page` → `save`, which opens a docs PR).

| File | Target page | Change |
|---|---|---|
| `guides/agent-routing-folders.mdx` | new: `guides/agent-routing-folders` | New guide: folders as per-team routing scopes (rules, files, evals, sharing, where folders apply). Add to the Guides nav next to **Route questions to the right agent**. |
| `guides/agent-routing.mdx` | existing: `guides/agent-routing` | Live page copied as-is, with folder additions: intro pointer, selector shows folders, "don't pick a folder" for the global rule, pin a folder in Step 5, new "Route within a team with folders" section, tips, two troubleshooting entries, related link. Existing images are kept. |

## Before publishing

- **Verify in the running app.** Both drafts were written from the code, not from a
  live stack. Check each claim and UI label against a seeded stack, as the
  docs-update skill requires.
- **Capture screenshots.** Each `{/* TODO screenshot */}` in the new guide needs
  an image under `/images/guides/agent-routing-folders/` (use the ui-evidence
  skill, seeded data only, 1440px wide).

## Behaviour the drafts rely on (code references)

- A folder-scoped report chooses among agents in the folder and **all visible
  subfolders**: `folder_branch_agents`, `backend/app/ai/tools/implementations/agent_focus_common.py:118`.
- Folder routing guidance is added to the roster: `FOLDER_ROUTING_GUIDANCE`,
  `backend/app/ai/context/agent_roster.py:32`; `backend/app/ai/agent_v2.py:2039`.
- Folder instructions load only for the **exact** picked folders; global ones
  always load: `instruction_scope`, `backend/app/services/folder_knowledge.py:30`.
- Attaching an instruction to a folder makes it non-global (`scope_mode="targeted"`):
  `backend/app/routes/folders.py:713`.
- Empty folder → `409 "No available agents in this folder"`: `agent_focus_common.py:304`.
- Slack/Teams/WhatsApp/embed expand a folder to its agents only (no folder
  instructions or files): `allowed_folder_agents`, `backend/app/services/folder_service.py:272`.
- Access inheritance (`inherits_access`): `backend/app/services/folder_service.py:8`.
