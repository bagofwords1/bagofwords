---
name: ui-audit
description: Exhaustively audit the UI control by control and role by role — enumerate every button, link, and input on a set of pages, write down what each is supposed to do (derived from the handler code and the RBAC matrix), then drive a real browser as each role and record what it actually does, filing every mismatch as a finding. Use whenever the user wants to hunt UI bugs broadly rather than fix one known bug — "go through every page and every button", "we have lots of weird UI bugs", "sweep the app for breakage", "check that these controls actually work", "verify what admins vs members can see", or when producing a bug inventory for triage.
---

# UI audit — control-level sweep

Compare intent to behavior, one control at a time, and write down both.

## Which skill is this?

Three skills overlap here; picking the wrong one wastes a sandbox boot.

| Skill | Unit of work | Use when |
|---|---|---|
| **`qa`** | User flows ("invite a member, they can sign in") | Release QA, post-merge smoke, "QA this area". Breadth across journeys. |
| **`ui-audit`** (this) | Individual controls | Hunting unknown bugs. Depth within pages — every button, including ones no flow touches. |
| **`sandbox-feedback-loop`** | One known bug | You already know what's broken and are fixing it. |

Findings from this skill feed `sandbox-feedback-loop` — audit, then fix, never both at once.
Once you start fixing you stop looking, and the pages after the first bug get a worse audit
than the pages before it.

## Why the phases are ordered the way they are

An LLM given a browser reports that everything works. Not from carelessness — from ordering.
It navigates, sees a page render, and forms its expectation *after* seeing the result, so
whatever happened becomes what was supposed to happen. A button that silently does nothing is
indistinguishable from a button that correctly did nothing.

So:

> **Write the expectation to disk before opening the browser**, derived from the handler code
> rather than the rendered page. The comparison then becomes a diff against a committed
> artifact instead of a judgment call made while looking at the answer.

That is the whole method. Phases 2 and 3 must not be interleaved.

## Phase 0 — Scope, roles, and boot

Pick a **page group** of 3–6 related routes, not the whole app. One group audited end to end
beats every page half-covered, and a group shares fixture state.
`references/bow-surface.md` lists the groups, the permission gating, and the traps — read it
before choosing.

Then pick the **roles** you will audit as. Most of this app's UI is permission-gated, so a
page audited only as admin is audited in its easiest state, and the whole class of "the UI
offered a control the backend forbids" goes unseen. `references/roles.md` has the matrix,
where it actually lives in the code, and verified seeding recipes. At minimum use admin plus
one non-admin; add a single-permission custom role when a finding hinges on one gate.

**Boot via the `sandbox-feedback-loop` skill** — it owns sandbox setup for this repo and
already encodes the traps (sqlite URL, onboarding dismissal, LLM provider seeding). Read it
and follow it rather than re-deriving. In short, it lands on the standard agent stack:

```bash
tools/agent/boot_stack.sh --dev              # backend :8000 + frontend :3000; --stop to tear down
cd backend && uv run python ../tools/agent/seed_org.py --demo
ANTHROPIC_API_KEY=$ANTHROPIC_KEY uv run python ../tools/agent/setup_haiku_llm.py   # only if auditing chat/report pages
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
```

Seeded admin is `admin@example.com` / `Password123!`. Seeding a second role needs the manual
recipe in `references/roles.md` — `seed_org.py --invite` currently 422s.

Logs land in `/tmp/bow-agent/{backend,frontend}.log` — keep the backend log open, it explains
most "the UI did nothing" findings, and it is where a silent 403 shows up. The database is
`backend/db/agent.db`, disposable by design, which matters because **this skill clicks
destructive controls**. Never point an audit at a database whose contents you care about.

## Phase 1 — Inventory (browser, read-only)

`scripts/sweep.mjs` visits each route without clicking anything and records the final URL
after redirects, every interactive control with a verified selector, console errors, uncaught
exceptions, and failed `/api/` responses.

```bash
node .agents/skills/ui-audit/scripts/sweep.mjs \
  --routes /settings/general,/settings/members,/settings/models \
  --login --out audit/settings
```

`--login` signs in through the UI and dismisses onboarding, caching the session for reuse.
Run with `--help` for the rest; `--storage none` sweeps logged out, which is how you audit
the auth pages.

**Sweep once per role**, into separate output directories — the session cache is per-`--out`,
so reusing one directory silently sweeps the first role twice and yields a confident, wrong
comparison:

```bash
BOW_EMAIL=member@example.com BOW_PASSWORD='Password123!' \
  node .agents/skills/ui-audit/scripts/sweep.mjs --routes /agents --login --out audit/agents-member
node .agents/skills/ui-audit/scripts/diff-roles.mjs audit/agents audit/agents-member
```

`diff-roles.mjs` reports controls present for one role and absent for the other. Admin-only
controls are usually correct gating; anything present for the *lower*-privileged role and not
for admin is almost always a bug in the gate's condition.

Three findings fall out before you click anything, and they cost nothing:

- **Route bounced** — you asked for `/x` and landed elsewhere. Either the route is dead or a
  guard is wrong. Check `references/bow-surface.md` first: an unverified user or undismissed
  onboarding redirects *everything*, and that is a broken fixture, not thirty bugs.
- **Console errors or uncaught exceptions on load** — a page that throws on mount is broken
  even when it looks fine.
- **Failed API calls during load** — a 4xx/5xx is a defect regardless of appearance, and if
  the UI doesn't surface it, that silence is the second and usually worse finding.

## Phase 2 — Expectation (code, no browser)

For each control, open the `.vue` file, find the bound handler, and follow it far enough to
name an observable outcome: a navigation, an API call, a modal, a toast, a mutated list.
Record one row per control in `expectations.md` and **commit it before Phase 3.**

```markdown
| # | Control | Selector | Expected | Source |
|---|---------|----------|----------|--------|
| 1 | "Save Provider" | `button:has-text("Save Provider")` | POST /api/llm/providers, success toast, modal closes, row appears in list | pages/settings/models.vue:214 |
| 2 | "Test Connection" | `[data-testid="test-connection"]` | POST .../test, shows "Successfully connected to LLM" | pages/settings/models.vue:188 |
```

Four cases carry most of the value:

- **No handler bound.** Expected outcome is "nothing". If it looks clickable to a user,
  that is already a finding — record it now and confirm in Phase 3.
- **Handler fires but ignores the result.** Expected is "no visible change"; the finding is
  that failures are invisible. This is a recurring bug class in this codebase.
- **Expectation depends on state** — empty vs populated. Write both rows. Empty states are
  the richest vein here and the least covered by the existing suite.
- **Expectation depends on role.** Add a `Role` column and one row per role whose answer
  differs. Take the expected answer from `permission_resolver.py` and the route's
  `definePageMeta`, never from what the UI renders — the UI's copy of the matrix is the thing
  under test. `references/roles.md` explains why that distinction has teeth.

Write `UNCLEAR` rather than guessing when the code doesn't answer in a couple of minutes. An
honest `UNCLEAR` is worth more than a fabricated expectation, and a control whose purpose
isn't legible from its own handler is worth flagging on those grounds alone.

## Phase 3 — Reality (browser, clicking)

Work down the table in order. Click, observe, fill the `Actual` column with what you saw —
not what it means. "Nothing happened, no network request, no console output" is an
observation; "the handler is probably missing" is a diagnosis that belongs in the finding.
Merging them is how a wrong root cause gets locked in early.

- **Confirm the click landed** before believing a no-op. A locator that matches a stale
  element clicks nothing at all and looks exactly like a broken button — the single most
  common false finding in an automated sweep.
- **Check the network and the backend log**, not just the DOM. A button that "does nothing"
  but fires a successful POST is a rendering bug; one that fires nothing is a wiring bug.
  Different owner, different fix, and this sweep is the only place separating them is free.
- **Destructive controls last**, on entities you created for the purpose — deleting fixture
  data mid-sweep invalidates every row after it.
- **Screenshot mismatches only.** A screenshot of correct behavior is noise. Use
  `tools/agent/capture.mjs` for one-off evidence outside the sweep.
- **Time-box to ~90 minutes per group.** Bug-finding accuracy falls off after that, and a
  tired sweep produces confident wrong rows that cost more to disprove than they were worth.

Run the role rows as the role, in its own browser context. When a control 403s for a
non-admin, the audit is not over: a 403 means the backend refused something the UI chose to
show, so the finding is the affordance, not the refusal. Check whether the UI surfaced
anything to the user — a silent 403 is worse than a rejected one.

## Phase 4 — Findings

Only rows where expected ≠ actual become findings; the rest stay in the table as proof of
coverage. Severity rules, the write-up template, and worked examples are in
`references/findings-format.md`.

| | |
|---|---|
| **P1** | Data loss, unrecoverable state, the page crashes, or a user acts beyond their permissions |
| **P2** | Control is broken: no-op, wrong destination, unhandled API error, endless spinner |
| **P3** | Works but wrong: stale data, wrong label, error swallowed, state not refreshed, a control offered to a role the backend forbids |
| **P4** | Cosmetic: layout, copy, untranslated string |

Write each finding in the `sandbox-feedback-loop` format — symptom → reproduction → suspected
root cause with `file:line` — so a confirmed one can be picked up and driven to a fix without
re-deriving it. That skill owns the fix; this one owns the finding.

## Deliverable

Follow the repo's report convention — file at `docs/feedback-loops/ui-audit-<date>-<group>.md`
alongside the existing feedback-loop docs, with the raw artifacts beside it:

```
docs/feedback-loops/ui-audit-<date>-<group>.md   # findings, P1 first
audit/<group>/expectations.md                    # every row, passes included
audit/<group>/routes/*.json                      # sweep output
audit/<group>/screenshots/                       # mismatches only
audit/<group>/state.json                         # session JWT — gitignored, never commit
```

`--login` banks a real session token in `state.json`. It is gitignored along with the raw
sweep output; keep it that way and commit only the markdown.

Lead the report with P1s and P2s. A reader who stops after the first screen should have seen
the worst thing you found; coverage goes last. Report the mismatch count and the P1/P2 list
in your message to the user, and don't fix anything unless asked — a triageable findings
document is the deliverable.
