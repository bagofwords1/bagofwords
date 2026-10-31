---
key: browse-web
title: Browse the web with the browser tools
description: Use when a task needs a real browser — JavaScript pages, clicking through a site, downloading a file — via browser_navigate, snapshot, act, extract and vision.
category: general
version: "1.0"
modes: [chat, training]
tags: [browser, web, research]
---

The browser tools drive one headless Chromium page per report, inside the
URL allowlist of the agent's browser connection. They are slow and every page
you read lands in your context, so the skill is picking the cheapest tool for
each step and never looping by hand over work that belongs in code.

## 1. Pick the tool before opening anything

| The task | Use |
|---|---|
| Read one static page / article / JSON URL | `web_fetch` — no browser needed |
| A page that only renders with JavaScript, or needs clicks, tabs, filters, a login-free form, or a download | the browser tools below |
| More than ~3 pages or ~20 rows (paginated lists, catalogs, "every item on the site") | `create_data` with the `browser` / `http` helper (scrape-with-code skill). Look at the page with the browser **once** for structure, then hand it to code |
| Check that an app or dashboard you built works | `browser_navigate` with `artifact_id` (no browser connection needed) |

No browser connection attached → `browser_navigate` with a `url` fails. Say
so and use `web_fetch` instead; do not retry.

## 2. The loop

1. **`browser_navigate(url)`** — opens the page and returns a `session_id`
   plus a first snapshot. Pass that `session_id` to every later call; omit it
   only to start over.
2. **Read before you act.** Want the content (article, table, report)?
   `browser_extract` — it returns readable text, which is far cheaper than a
   snapshot. Want to interact? Use the snapshot, where every control has a
   ref like `[ref=e12]`.
3. **`browser_act(ref, action, text)`** — `click`, `type` (text in `text`),
   `press` (key such as `Enter` in `text`), `select` (option label/value in
   `text`), `hover`, `scroll`. It returns a fresh snapshot.
4. **Refs expire** whenever the page navigates or its DOM changes. A
   `stale_ref` error means: call `browser_snapshot`, take the new ref, act
   again. Never reuse a ref from an older snapshot.
5. **`browser_vision`** only when the layout itself is the question (a
   chart, "why does this look wrong", something the text tree cannot show).
   It costs an image; text and snapshots do not.

Keep snapshots small: the default (`full=false`, ~8k chars) shows interactive
elements only. Ask for `full=true` or more `max_chars` only when the element
you need is missing.

## 3. Long pages and files

- `browser_extract` returns up to `max_chars` inline. A longer page comes back
  `truncated` with a `text_file_id` holding the **full** text — read the part
  you need with `read_file` instead of raising `max_chars` to the limit.
- Files the page downloads come back under `downloads` as report files. Read
  them with `inspect_data` or `read_excel_as_csv`, and build tracked data from
  them with `create_data` like any uploaded file.
- A `browser_vision` screenshot is a report file too: its `screenshot_file_id`
  can be embedded in an artifact.

## 4. When the page says no

Check `blocked_reason` on every result:

- **`authentication`** — the page wants a login. Stop and tell the user.
  Never type credentials, guess passwords, or look for a way around it.
- **`captcha`** — stop and tell the user. Never try to solve or bypass it.
- **`allowlist`** / `not_allowed` — the URL is outside the browser
  connection's allowlist. Tell the user which host was refused; an admin can
  add it to the connection. Do not try other URLs to reach the same content.

Secret-shaped inputs (passwords, tokens, card numbers) are masked in
snapshots and screenshots. Do not ask the user to paste secrets into chat so
you can type them.

## 5. Report what you saw

- Quote numbers and facts from the page text you just read, with the URL
  (and page title) they came from. Do not fill gaps from memory: if the page
  did not show it, say it was not there.
- Say what you could not reach (login wall, blocked host, missing element)
  instead of leaving it out silently.
- Summarize the steps you took in one or two lines; the user can see the
  tool calls, they do not need a click-by-click replay.
