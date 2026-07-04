# Repro harness — /agents connections footer overflow

Isolated, offline reproduction of the `/agents` connections-footer overflow.
See `../../sandbox-feedback-loop-agents-connections-overflow.md` for the full
write-up. This folder is the runnable loop.

- `repro.html` — the connections footer from `KnowledgeExplorer.vue` (lines
  219-242), classes copied verbatim, `<UTooltip>` wrappers modelled as
  `<div class="relative inline-flex">` (Nuxt UI v2 wrapper). `?w=` sets the tree
  pane width (default 300, clamp 220-600), `?n=` sets `connections.length`.
- `shot.mjs` — drives Chromium, waits for Tailwind to apply, measures footer
  overflow + how far "View all" spills past the pane edge, writes PNGs.

## Run

```bash
# 1. Playwright module (browsers are pre-installed in the sandbox)
npm install playwright                       # skips browser DL if present

# 2. Tailwind Play CDN, downloaded locally (the headless browser has no proxy,
#    so an inline <script src="https://..."> would never load). gitignored.
curl -sL https://cdn.tailwindcss.com -o tailwind.js

# 3. Screenshots + measurements
CHROME_BIN=/opt/pw-browsers/chromium-1194/chrome-linux/chrome node shot.mjs
```

Only these core utilities matter here (`flex`, `gap-2`, `w-6`, `px-3`,
`text-[11px]`, `me-1`, `h-6`, `px-1.5`) — all standard Tailwind scale the
project does not override — so the CDN engine reproduces the real layout.
