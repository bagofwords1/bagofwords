# Findings format

How to write up a mismatch so someone can triage it without re-running the audit.

## The rule

A finding needs three things: what you expected and where that expectation came from, what
actually happened, and enough detail for someone else to see it again. Missing the source
makes it an opinion; missing the reproduction makes it a rumor.

## Template

```markdown
### [P2] "Save Provider" silently discards the form on a 409

**Route:** `/settings/models`
**Control:** `button:has-text("Save Provider")` (expectation row #7)

**Expected:** POST `/api/llm/providers`, success toast, modal closes, provider appears in
the list. — `frontend/pages/settings/models.vue:214`

**Actual:** POST returns 409 (duplicate name, including soft-deleted providers). The modal
closes anyway, no toast appears, and the list is unchanged. The user sees a form that
submitted successfully and a list that did not update.

**Reproduce:**
1. `/settings/models` → "Integrate Models" → "New Provider" → anthropic
2. Name it `Anthropic` (any name previously used and deleted)
3. Fill the API key, click "Save Provider"

**Evidence:** `screenshots/settings-models-save-409.png`, `inventory/settings_models.json`
**Note:** The 409 itself is expected behavior (names are unique across soft-deleted rows).
The finding is that the failure is invisible.
```

The `Note` line is where an audit earns its keep: separating "the backend is right and the UI
lies about it" from "the backend is wrong" is most of the triage work, and you are the only
one who saw both layers.

## Severity

Assign from consequence to the user, not from how surprising the bug is.

**P1 — Data loss, unrecoverable state, or crash.** Work is destroyed, the user is locked out,
or the page throws and stops rendering. A destructive action that fires without confirmation
is P1 even if it "worked".

**P2 — The control is broken.** No-op, wrong destination, an API error that the UI never
surfaces, a spinner that never resolves, a form that cannot be submitted. The user cannot
complete the task the control exists for.

**P3 — Works but wrong.** The action succeeds and something about the result is incorrect:
stale list after a mutation, wrong label, a count that does not update, an error surfaced in
a way that misdescribes it. The task completes; the user is misinformed.

**P4 — Cosmetic.** Layout, overflow, copy, untranslated strings. Real, but nobody is blocked.

Two calls that come up constantly:

- **A disabled control is not a bug** unless the conditions for enabling it are met. Check
  the gating logic in the handler before filing. (See the send-button gating in
  `bow-surface.md`.)
- **A console error with no user-visible effect is P3, not P2** — but do file it. These are
  the leading indicator for the P1 that shows up later under different data.

## Not findings

Recording these as bugs makes the document harder to act on, which costs more than the
coverage gains:

- Features that are visibly unbuilt (empty page, "coming soon").
- Flows that stop at a credential wall you cannot pass — record the boundary, not a failure.
- Dev-server console noise: HMR, Vue devtools hints, source map warnings.
- Anything caused by your own fixture being wrong (unverified user, undismissed onboarding).
  Fix the fixture and re-run.
- Behavior you disagree with but that matches the code and has no user-facing defect. If it
  is worth saying, put it in a short "Observations" section at the end, clearly separated
  from the findings.

## Document structure

```markdown
# UI audit — <group> — <date>

## Summary
<n> controls checked across <n> routes. <n> mismatches: <n> P1, <n> P2, <n> P3, <n> P4.

## P1
## P2
## P3
## P4

## Observations
Non-defect notes worth someone's attention.

## Coverage
Routes swept, routes skipped and why, controls marked UNCLEAR in Phase 2.
```

Lead with P1 and P2. A reader who stops after the first screen should have seen the worst
thing you found. Coverage goes last — it proves the audit was real, but nobody opens a bug
document to read about what was fine.

## Worked example: a no-op

The most common real finding, and the easiest to get wrong:

> **Expected:** Opens the schema editor modal. — `pages/agents/index.vue:88`
> **Actual:** Click registers (button shows its active style), no modal, no network request,
> no console output. Repeated on reload and in a fresh session.

Note what makes this credible: the click was confirmed to land (active style), three
observation channels were checked (DOM, network, console), and it was reproduced twice. A
no-op report without those is indistinguishable from a missed selector — which is the single
most common false finding in an automated sweep, because a locator that matches a stale
element silently clicks nothing at all.
