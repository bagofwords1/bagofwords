# Feedback Loop — "Scrape every page into a list without the model reading every page"

A user asks the agent to scrape a paginated, JavaScript-rendered site into an
Agent List. Before this change the only way was the agent's own browser tools,
one call per page, with every page's text going through the model, and then
`submit_<list>` with the model writing every row. The claim validated here: with
the org's new **Sandbox** settings on, one `create_data` call does the whole job
in code (browser, pagination, list writes). The model sees counts and a preview,
and the cost stays roughly flat as the number of pages grows.

## What changed (validated)

- Settings, all off by default (`backend/app/schemas/organization_settings_schema.py`):
  `enable_sandbox_web_fetch` (+ `sandbox_web_fetch_max_urls`),
  `enable_sandbox_browser` (+ `sandbox_browser_allow_mutating_requests`,
  `sandbox_browser_max_pages`), `enable_sandbox_lists` (+ `sandbox_lists_max_rows`).
  Migration `sbxweb01` turns code web fetch on for orgs that already had
  `enable_web_fetch` on, so their existing steps keep working.
- Trusted-side helpers: `backend/app/ai/code_execution/sandbox_helpers.py`
  (`MeteredHttp`, `SandboxBrowser`, `SandboxHelpers`) and
  `backend/app/services/agent_lists/sandbox_sink.py` (validate on submit, hold,
  save after success). Code-facing objects: `ListsHelper` / `BrowserHelper` in
  `sandbox/namespace.py`; RPC methods `lists_submit`, `lists_available`,
  `browser` in `sandbox/runner.py` / `sandbox/child.py`.
- Only the agent's own `create_data` passes `browser` / `lists`
  (`create_data._build_sandbox_helpers`); reruns and dashboard refreshes build
  the executor without them.
- Planner routing (`prompt_builder_v3.py`, "Bulk scraping runs in code"): the
  first live run showed that without this rule the planner ignores the helpers
  and loops its own browser tools.

## Loop A — deterministic (no external services)

```bash
cd backend
export BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
uv run python -m pytest -p no:cacheprovider -q \
  tests/unit/test_sandbox_helpers.py \
  tests/unit/test_sandbox_browser.py \
  tests/unit/test_planner_sandbox_routing.py \
  tests/unit/test_sandbox_web_fetch_migration.py \
  tests/unit/test_report_web_fetch_sandbox.py
uv run python -m pytest -p no:cacheprovider -q -m e2e --db=sqlite tests/e2e/rbac/test_sandbox_list_writes.py
```

`test_sandbox_browser.py` drives real Chromium against a local site: it reads
JS-rendered items, extracts per-item fields, follows links, enforces the page
budget (clicks count), stays inside the allowlist, and blocks a form POST
unless "Allow form submissions from code" is on. Mutation-checked: turning off
the POST block or the per-attempt reset makes those tests fail.

## Loop B — live (real model, real site)

Stack: `tools/agent/boot_stack.sh --dev`, seeded with `tools/agent/seed_org.py`.
An Anthropic provider (Claude Haiku 5.5), a `browser` connection allowlisted to
`https://quotes.toscrape.com/**`, and a list `Quotes` (key `text`; `author`,
`tags`). The key is supplied through an environment variable only. Prompt:

> Scrape every quote from https://quotes.toscrape.com/js/ — the page is rendered
> with JavaScript and has a Next button, so go through all pages. Save each
> quote (text, author, tags) to the Quotes list.

Token counts are from `llm_usage_records` for the report.

| Run | Rows saved | Wall time | Tool calls | LLM calls | Input tokens (incl. cache reads) | Output tokens | Cost |
|---|---|---|---|---|---|---|---|
| Sandbox off (before) | 100 / 100 | 104 s | 22: browser_navigate, 9× browser_act, 10× browser_extract, 2× submit_list, create_data | 17 | 1,336,960 | 19,282 | $0.053 |
| Sandbox on | 100 / 100 | 20 s | 1: create_data | 3 | 92,003 | 1,031 | $0.017 |
| Sandbox on (repeat) | 100 / 100 | 23 s | 2: browser_navigate, create_data | 4 | 168,216 | 1,287 | $0.019 |

With the settings on, the generated code loaded 10 pages, extracted every quote
with one `query_all(".quote", fields={...})` per page, and called
`lists.submit("quotes", rows)` per page. The model got back
`sandbox_activity: {browser_pages_loaded: 10, lists: {inserted: 100, ...}}`.

Issues the live loop found and their fixes:
1. Without the planner rule, the agent kept using its own browser tools and
   `submit_list`. One run saved only 50 of 100 rows in 306 s.
2. On SQLite, saving from a separate session hit "database is locked" while
   the agent's session held a write. The save now takes `tool_db_lock` and
   commits the agent's session first, the same discipline as the tool's other
   writes.
3. Flat `query_all` lists made per-item extraction awkward (tags came out
   empty). `query_all(..., fields={...})` now returns one dict per item.
4. `fields={"href": "a@href"}` on an `<a>` read a descendant and found nothing,
   so pagination stopped after page 1 (the agent noticed and reran). A
   selector the element itself matches now reads the element.

## What this proves / regression notes

- Rows reach the list only after a successful run. A failed attempt's rows are
  discarded before the retry, an invalid value raises inside the code, and
  access is checked again at save time.
- The page budget, the URL allowlist and the POST block are enforced on the
  trusted side, whatever the code does.
- The quotes site is small (10 pages). The before-path cost grows with every
  page; the after-path cost stays about the same.
- `chromium_executable()` now also finds Playwright's newer `*-linux64` browser
  layout (`/opt/pw-browsers` in cloud sandboxes).
