# Source links: SharePoint / Outlook / monday.com back to the original

**Ask:** participants want a link back to the source (SharePoint document,
email, monday item) in BOW answers.

## Changes

| Layer | Change |
|---|---|
| `backend/app/data_sources/clients/monday_client.py` | Items fetch `url` → new `item_url` column on every board table/DataFrame; boards fetch `url` → table description (`url: …`) + `metadata_json.board_url`; client prompt tells the agent to keep `item_url` and cite the board link for aggregates. |
| `backend/app/ai/agents/planner/prompt_builder_v3.py` | Analytics standard: end answers built on a file/email/item with markdown links (`[label](web_url)`), never bare or invented URLs. SharePoint/OneDrive/Outlook/Gmail/OneNote already return `web_url`. |
| `frontend/components/AgGridComponent.vue` | Generic cell renderer: a cell whose value is a single http(s) URL renders as a localized "Open ↗" link (new tab, `noopener`), built as a DOM node (no innerHTML). Covers chat result tables and dashboard tables. |
| `frontend/pages/reports/[id]/index.vue` | Chat markdown links were styled as body text (`text-gray-900 no-underline`) — indistinguishable from prose. Now blue + underlined; hover icon uses `inset-inline-start` so it sits correctly in RTL. |
| `locales/{en,es,he}.json` | `common.openLink`. |

## Loop

1. Booted backend (sqlite) + Nuxt dev, seeded org via `tools/agent/seed_org.py`.
2. Uploaded `sources.csv` (SharePoint / Outlook / monday URLs) and sent a chat.
   **The sandbox Anthropic key had no credit** ("credit balance is too low"), so
   the agent turn could not run live.
3. Seeded the agent reply directly in the DB (a `create_data` tool block with a
   table step + a final markdown block with source links) to exercise the real
   report page rendering.
4. Screenshots in `media/pr/feat-source-links/`, en + he, before/after.
   "Before" table shots were taken with `AgGridComponent.vue` reverted to HEAD.
5. DOM check: chat links are `<a target="_blank" rel="noopener noreferrer">`
   (markstream default) — before the fix computed color `rgb(17,24,39)`,
   `text-decoration: none`.

## Tests

- `tests/unit/test_monday_client.py` — 26 passed (updated column expectations,
  new assertions for `item_url`, `board_url`, description url).

## Not verified

- Live LLM behaviour of the new prompt rule (no LLM credit in sandbox).
- Real monday.com / SharePoint / Outlook tenants (URL fields come from the
  documented `Item.url`, `Board.url`, Graph `webUrl` / `webLink`).
