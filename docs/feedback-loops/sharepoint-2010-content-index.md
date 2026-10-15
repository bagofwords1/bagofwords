# Feedback Loop — SharePoint 2010 content index and catalog-first listing

**Status: executed 2026-10-15 against a synthetic SharePoint 2010 served over
real HTTP; one round of defects found and fixed; final run clean.** PR
[#1387](https://github.com/bagofwords1/bagofwords/pull/1387).

Claim being validated: on a service-account SharePoint 2010 connection with
`index_mode: content`, the agent can browse a folder sorted by created or
modified date, find documents by a term that appears only in their bodies,
and page through the whole library, all from the catalog, with live SharePoint
reads only for file contents or on explicit request. The agent must also see
how fresh the catalog is.

Model: Anthropic Claude Haiku 5.5 (`claude-haiku-5-5`), org default. The key
is read from the environment only and never written anywhere.

---

## Loop

1. **SharePoint 2010 stand-in.** `tests/unit/test_sharepoint_2010_client.py`'s
   `LegacyServer` (synthetic MS-LISTSWS / MS-COPYS responses) wrapped in a
   `ThreadingHTTPServer` on `127.0.0.1:8010`, serving one library of 73 text
   files under `Finance|Legal|HR / 2023..2026`, each with its own `Created` and
   `Modified` stamps. The backend talks to it over real HTTP with NTLM
   credentials (`allow_http: true`).
2. **Stack.** Backend (`uv run python main.py`, pinned `BOW_ENCRYPTION_KEY`),
   frontend (`yarn dev`), org + user + Anthropic provider seeded through the
   real API, then `POST /api/connections` (`sharepoint_onprem`, `api_mode: 2010`,
   `index_mode: content`, `auth_policy: system_only`), `POST /api/data_sources`,
   `POST /api/connections/{id}/reindex`, polled on `/indexing`.
3. **Chat through the UI (Playwright).** Three turns on one report:
   - "List all files in the Legal/2025 folder, sorted by created date, newest first. Show the created date of each."
   - "Which documents mention arbitration? List their paths."
   - "What is the most recently modified file anywhere in the Finance folder? Give its path and modified date."
4. **Verify below the UI.** `connection_tables` rows (keywords, timestamps,
   `indexed`), `connection_indexings` (one completed shared run),
   `tool_executions` (tool inputs and model-facing summaries), the backend log
   (real `POST https://api.anthropic.com/v1/messages` 200s, no errors), and the
   rendered schema context via `SchemaContextBuilder` with `import main`.

## Observed — index

| Check | Result |
|---|---|
| First content index, 73 files | completed, 73/73 rows, every row with keywords, `created_at`, `modified_at`, `indexed: true` |
| Second index (incremental) | completed in 1.1 s, 73/73, no document downloads |
| Schema context | `<index mode="content" files="…" capped="false" indexed_at="2026-10-15T10:05:20Z"/>` |

## Observed — round 1 (defects)

| Turn | What the model did | Root cause |
|---|---|---|
| 1 | Catalog listing was correct and sorted, but the model said "neither the catalog nor the live listing returned a created_at", re-listed live, then gave an alphabetical table with no dates | The model-facing inventory rows (`_inventory_row`) carried name/path/size/id only; timestamps were in the output the planner never reads |
| 1 | Live re-list with `folder_id="Legal/2025"` failed: "Path is outside the configured document library/folder" | The live SharePoint path accepted only server-relative URLs; the catalog path accepted relative folders, so the same id worked on one path and not the other |
| 2 | `search_files(deep=true)` returned 8 files: the ones named `arbitration_*`; the 8 `contract_*` files whose bodies say "Arbitration clause applies" were missed | `deep=true` skipped the keyword index entirely, and SharePoint 2010's live search matches filenames only, so "deep" was a downgrade |
| 3 | Live listing with `sort_by=modified_at`: model reported "the result has no modified dates … alphabetical, not by modified date" | Same row problem as turn 1, plus the live path silently ignored `sort_by` |

Fixes (commit 3 of the PR): timestamps in every inventory row; relative folder
ids resolved to the library scope on the live path for both API modes;
`deep=true` unions the live scan with the keyword-index hits and the summary
says "keyword index … + live scan"; the live path sorts a complete scan and
says plainly when sorting was not applied to a partial one.

## Observed — round 2 (after fixes)

| Turn | Tool call recorded | Summary the model saw | Answer |
|---|---|---|---|
| 1 | `list_files(folder_id="Legal/2025", recursive, sort_by=created_at desc)` | `Listed 6 file(s) (catalog) … indexed 2026-10-15T10:05:20Z; 6 match; sorted by created_at desc; rows carry modified/created times` | Six files, newest first, created dates shown, index time stated, zero SharePoint calls |
| 2 | `search_files(query="arbitration", deep=true)` | `Found 16 file(s) … (keyword index, indexed … + live scan)` | All 8 `arbitration_*` and all 8 `contract_*` files, correctly split into name matches and body matches |
| 3 | `list_files(folder_id="Finance", recursive, sort_by=modified_at desc, source="live")` | `Listed 24 file(s) (live) Sorted by modified_at desc.` | Newest file with its modified and created time; the model flagged that the synthetic dates lie in the future |

Backend log: 20 Anthropic responses, all 200; no errors.

## Not verified here

- A real SharePoint 2010 farm (the `Created` field and UTC date format follow
  the protocol spec and the synthetic server only).
- 25,000-file scale timings.
- The connection form rendering of the new `content` option.

## Rerun

```bash
# fake SP 2010 on :8010 — see the scratch script in the PR discussion, or
# adapt tests/unit/test_sharepoint_2010_client.py::LegacyServer behind
# http.server; then boot the stack per .claude/skills/sandbox-feedback-loop.
```
