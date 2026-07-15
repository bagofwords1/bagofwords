# Issue Schema — how to open a GitHub issue in bagofwords

An issue is a **dual-audience artifact**, and every requirement below falls out of that one fact:

1. **A maintainer** must be able to *decide* — is this real, how bad, do we fix it now — from the top of the issue, without reading the code themselves.
2. **A future fixer** (human or agent, often months later) must be able to *reproduce it, find the exact lines, and know what "right" looks like* — so the issue doubles as a durable repair map.

Write for both. This is the sibling of `PR_DESCRIPTION_STANDARD.md`: a **PR proposes a change**; an **issue reports a problem** (and optionally proposes the fix). Where they overlap — product-altitude-first, evidence-not-assertion, radical honesty about scope — they agree by design.

Everything here is grounded in the CLAUDE.md cornerstones, restated for issues:
- **Evidence over assertion** (the "verify FACTS" carve-out): quote real output, cite `file:line`, trace the hop — never "I think it probably…". A claim a reader can't check is not evidence.
- **Concrete & self-explaining**: name the persona, the action, the exact file — no bare codes, no "the thing breaks."
- **Product altitude before mechanism**: lead with how it hurts a paying customer in plain language; the code comes second.
- **No severity-by-vibe** (No-Scores cornerstone): the bracket tag is a *judgment stated in words in the body*, not a number pulled from feeling. Justify it with the impact, don't inflate it.
- **Candor about scope**: say what is NOT affected, what is NOT in scope, and — for a bug surfaced by a PR — that it is NOT caused by that PR.

---

## 1. Title — `[Severity] area: precise outcome`

The convention the real issues use (#580–#587):

- **`[Severity]`** in brackets, first: `[Low]`, `[Low-Medium]`, `[Medium]`, `[Medium-High]`, `[High]`. Compound tags are fine when the call is genuinely between two levels — that honesty beats false precision.
- **For infra/CI/tooling**, replace the bracket with a domain prefix: `CI: …` (#640), `chore: …`, `test: …`. Same idea — the reader knows the class at a glance.
- **`area: precise outcome`** — the *outcome*, not the file. Include the **scope count** when the bug repeats (`…across 11 frontend sites`) and the **intended behavior** in a parenthetical when it clarifies (`…hard-fail on missing OPENAI_API_KEY_TEST (should skip)`). **Keep the count consistent with the enumerated Affected list** — #584's title says "11 sites" but its body lists 14; don't do that.

**Done test:** a maintainer scanning the issues list knows the severity, the area, and what actually goes wrong — without opening it.

**Good**
```
[Medium] Delete/remove/toggle actions silently no-op on backend failure across 11 frontend sites
[High] Monitoring tab silently shows an empty dashboard instead of an error for scoped users
CI: fork/external PRs can never pass `e2e-tests` — LLM tests hard-fail on missing OPENAI_API_KEY_TEST (should skip)
```
**Bad**
```
Bug in members panel            # no severity, no mechanism, vague area
Fix useMyFetch                  # a proposed fix as a title; states no problem
[Critical] everything is broken # severity-by-vibe, no outcome
```

Choosing the severity (in words, then tag it): **[High]** = silent data/security/billing wrongness a user acts on (a scoped user concludes "all fine" while 403s hide everything — #582; a "sent" notification that never sends — #585). **[Medium]** = a real failure the user is misled about but the blast radius is bounded (silent delete no-op — #584). **[Low]** = a hidden-affordance or cosmetic-but-real correctness gap (an unregistered permission string hides a debug button — #583). If you're between two, write the compound tag; don't round up to look urgent.

---

## 2. Required sections (each with its one-line "done test")

Order them as listed — that ordering *is* the progressive-disclosure requirement (feel the harm → trust it's real → find the lines → know the fix). A reader stops at any depth with a correct picture.

### `## Summary / Impact` — FIRST, always, at the altitude the harm actually lives
- **What it must contain:** lead where the harm is *real* — don't force a screen or a persona onto a bug that has neither.
  - **For a bug a user SEES** (UX / frontend): a **named persona** (data analyst, org admin, contractor with single-data-source access) doing a **concrete action** ("clicks Save changes on the training-instructions pill"), **what they see** ("the pill reverts to exactly how it looked before — bit-for-bit identical"), and **why it hurts** in product terms (they believe their approved fix is live; it isn't). No code here. Inline before/after ASCII is encouraged (see #580, #581, #582, #587).
  - **For an infra / CI / backend / test bug with no screen** (e.g. #640): a plain **Summary** of what is broken, then **who is structurally affected** ("external contributors can never get a green `e2e-tests`"). Do **not** invent a persona or a screen that isn't there.
- **Done test:** the reader who matches the altitude — a non-engineer for a UX bug, a maintainer for an infra bug — grasps the stakes from this section alone, without a fabricated user or screen.

### `## Root cause` (or `## Technical detail`) — the single mechanism, with `file:line`
- **What it must contain:** the **one mechanism** behind the symptom(s) where honest to collapse them — #584 titles it "Root cause (single mechanism behind all 11 sites)" and points at `frontend/composables/useMyFetch.ts (~L48-69)`. Every claim carries a **`repo-relative-path:line`** citation.
- **Done test:** a fixer can open the named line and see the cause with their own eyes — no further hunting to locate it.

### Evidence — quoted real output, never a paraphrase
- **What it must contain:** the **actual observed artifact** — a pasted test log (#640: `7 failed, 725 passed, 35 skipped, 1091 deselected` / `FAILED …test_llm_providers - Failed: OPENAI_API_KEY_TEST is not set`), a traced call chain "confirmed hop-by-hop (file + line)" (#582), the real error string the user sees (#581: `column "revenu" does not exist`). If you ran it, quote it; if you traced it, show the hops.
- **Done test:** nothing load-bearing rests on "I think" or "probably" — a skeptic can re-run or re-trace every factual claim.

### The "tell" — the proof it's a real bug, not intended behavior *(when one exists)*
- **What it must contain:** the **internal inconsistency** that rules out "working as designed." #640's tell: the *Azure* branch of the same fixture `pytest.skip`s when its key is absent while OpenAI/Anthropic `pytest.fail` — "Azure clearly shows the intended pattern is skip-when-absent; the others hard-fail — almost certainly an oversight." A correct sibling in the same file is the strongest tell.
- **Done test:** a maintainer inclined to say "that's just how it works" is answered by the issue itself.

### Scope & who's affected — enumerated, and honestly bounded
- **What it must contain:** the **full affected list** with `file:line` and the handler/symbol name (#584 lists all 14 sites), **who is hit** (#640: "external contributors are structurally locked out of a green `e2e-tests`"), and — the candor requirement — **what was checked and is NOT affected** (#584: "~15 other delete/toggle handlers … were checked and confirmed to already correctly gate on `error.value` — a real, bounded pattern … not a codebase-wide problem").
- **Call out the worst instance** when the affected sites differ in severity — #584 flags `FileUploadComponent` as the worst ("no rollback path exists at all"). It drives fix prioritization.
- **Done test:** the reader knows the exact blast radius and trusts it because the boundary is stated, not implied.

### A correct reference pattern — what "right" already looks like in-repo *(when one exists)*
- **What it must contain:** the **existing in-house template** the fix should copy — #584 points to `frontend/components/ReviewFeed.vue`'s snapshot-then-revert (`items.value = prev`); #640 points to the Azure skip branch. This turns "someone should fix this" into "copy that."
- **Done test:** the fixer has a concrete pattern to mirror, not a blank page.

### `## Proposed fix` — minimal first, then fuller/optional
- **What it must contain:** the **smallest correct change** first (#640: "change the three `pytest.fail(…)` → `pytest.skip(…)` at lines ~99, 138, 194"), then optionally a **fuller/root-cause option with its tradeoff named** (#584: fix the composable = kills the whole footgun vs. patch 14 sites = smaller blast radius but error-prone), and any **guardrail so it can't silently return** (a lint rule / review-checklist item). Defer a big design exploration to a follow-up comment rather than bloating the issue (#640 does exactly this) — and give that comment a shape: the options with their tradeoffs named, a single **worded recommendation** ("ship this"), and a **why-it's-safe** paragraph (see #640's follow-up comment).
- **Done test:** a fixer could start today from the minimal option; the tradeoffs of going further are explicit, not left to guess.

### Provenance footer — how it was found, and its relations
- **What it must contain:** an italic footer stating **how it surfaced** and **related issues/PRs**, including the real-bug-vs-artifact distinction. #584: "_Surfaced during a dedicated sweep for optimistic-UI/fire-and-forget silent failures…_". #640: "_Found while validating PR #589 (issue #584 fix). **Not caused by that PR — surfaced by it.**_"
- **Done test:** a reader knows whether this is a fresh regression, a pre-existing latent bug, a sweep finding, or test-rot — and which PR/issue it relates to.

### Reconstructed wireframe / before-after visual — **when, and only when, it's a UX or visual bug**
- **When warranted:** the bug is something a user *sees* on a screen — a silent no-op, a misleading toast, an empty-state that masks a 403, a reverting pill, a hidden button. Then include a **code-derived** wireframe like #584/#582: dated, stating the exact files+lines it was reconstructed from, with **Level 1 (where it sits on the real screen)** and **Level 2 (before / immediately-after / next-reload)** so the *invisibility* of the failure is legible.
- **When NOT warranted:** a CI/test/backend-logic/permission-registry bug with no distinct on-screen state (#640 has none; #583 is UX-adjacent but a one-line registry fix — a small ASCII suffices). Don't manufacture a wireframe where there's nothing visual to show.
- **The visual must be code-derived, not imagined:** cite the component and the template line-range, **and paste the ~5–15 line handler itself, annotated with the one line where the bug lives** — as #584 pastes `removeMember` and points at the dead `catch` that never fires. An invented mock is an assertion, not evidence.
- **If the bug repeats across N sites, say the one wireframe stands in for all N** (they share the identical shape), so fixing the shape once fixes all — as #584's "Same shape, 13 more sites" closer does; this is what justifies a root-cause fix over per-site patching.
- **Done test:** for a visual bug, a reader who never opens the app can see *why the user can't tell success from failure*; for a non-visual bug, no visual is forced in.

---

## 3. The generative questions (fill each section by answering these)

| Section | The question the author answers |
|---|---|
| Summary / Impact | UX bug: "Which real user, doing what, sees what — and why does it hurt (trust / money / security / data)?" · Infra/CI/backend bug: "What is broken, and who is structurally affected?" |
| Root cause | "What is the *one* mechanism, and at which `file:line` can I point to it?" |
| Evidence | "What did I actually observe or trace — quoted, not paraphrased?" |
| The tell | "What inconsistency proves this is a bug and not intended behavior?" |
| Scope | "Exactly what is affected, who is hit, and what did I check and rule OUT?" |
| Correct reference | "Where does the right pattern already live in this repo?" |
| Proposed fix | "What's the smallest correct change, and what does going further buy or cost?" |
| Provenance | "How was this found, is it caused-by or merely surfaced-by a PR, and what's related?" |
| Wireframe | "Is this something a user *sees*? If yes, draw the before/after from the code. If no, skip it." |

---

## 4. Paste-ready skeleton

```markdown
# [Severity] area: precise outcome (scope count if it repeats)

## User experience impact

<Named persona> doing <concrete action> sees <exactly what appears on screen>.
Why it hurts: <trust / money / security / data, in plain language — no code>.

<optional inline before/after ASCII for a UX bug>

## Root cause  <!-- single mechanism where honest -->

<the one mechanism>, at `path/to/file.ext:LINE`.

**Evidence:**
```
<pasted real log / error string / traced hop-by-hop file:line — not a paraphrase>
```

**The tell:** <the internal inconsistency proving it's a bug, e.g. a correct sibling in the same file>.  <!-- omit if none -->

**Affected (N confirmed):**
- `path/one.ext:LINE` — <handler/symbol>
- `path/two.ext:LINE` — <handler/symbol>

**NOT affected / already correct:** <what you checked and ruled out — bound the blast radius>.

**Correct reference pattern (already in-repo):** `path/to/good.ext` — <what it does right>.  <!-- omit if none -->

## Proposed fix

1. **Minimal:** <smallest correct change, with exact file:line>.
2. **Fuller (optional):** <root-cause option> — tradeoff: <what it buys vs costs>.
3. **Guardrail:** <lint rule / review-checklist item so it can't silently return>.

<!-- UX/visual bugs only: a dated, code-derived wireframe (Level 1 where-on-screen, Level 2 before/after/reload), citing the files+lines it was reconstructed from. Omit entirely for CI/backend/logic bugs. -->

---
_Provenance: <how found>. <caused-by vs surfaced-by which PR>. Related: #NNN, #NNN._
```

---

## 5. Where this belongs & how to enforce

Consistent with what already exists in this repo — **no issue standard exists today** (checked: root `AGENTS.md` + `frontend`/`backend` sub-guides reference skills and a commit-guidelines doc but say nothing about issues; global CLAUDE.md governs commits, not issues; auto-memory has no issue memory), and there is **no `.github/ISSUE_TEMPLATE/`** yet. Mirror how the PR standard proposes to enforce itself:

- **`.github/ISSUE_TEMPLATE/bug_report.md`** — the section 4 skeleton as a GitHub-native template, so every human-opened issue starts pre-shaped and GitHub auto-populates it. (A second `feature_request.md` can follow later; this schema is bug/finding-shaped, which is what the real corpus is.)
- **A reference from `AGENTS.md`** (and/or the docs standard set alongside the commit-guidelines doc) so **agent-authored** issues — the bulk of the #580–#640 corpus — carry this by default, the same way commits carry the commit-message guidelines.
- **The test for a good issue:** could a maintainer decide (real? how bad? fix now?) from the first screen, and could a future fixer reproduce it, find the exact lines, and copy the right pattern — without help? If either fails, it's not done.
