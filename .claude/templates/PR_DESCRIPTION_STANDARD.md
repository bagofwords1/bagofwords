# PR Description Standard — the generative logic

A PR description is a **dual-audience artifact**, and every requirement below falls out of that one fact:

1. **A human reviewer** must be able to *decide* (merge / request-changes) and know *what to check* — from the top of the page, without scrolling into the diff.
2. **A future agent or engineer** (including Claude Code) must be able to *navigate the change and follow the convention it set* months later — so the description doubles as a durable context/routing map.

Write for both. If a section serves neither, cut it. If either audience is missing something to do their job, it's incomplete.

---

## The 9 required elements (each states WHAT it must contain and the DONE test)

Order them top-to-bottom as listed — that ordering *is* the progressive-disclosure requirement (decide → see → trust → navigate → deep-detail). A reader stops at any depth with a coherent, correct picture.

### 1. Title + issue link
- **What:** `type(scope): outcome-in-imperative (#NNN)`, and a closing keyword (`Fixes #NNN` / `Closes #NNN`) in the body so the issue auto-links and auto-closes.
- **Done when:** the title says the *outcome*, not the file list; the issue number is one click away.

### 2. Two-line orientation (what + why)
- **What:** one line on *what changed* and one line on *the root cause / why it existed*, before any other prose.
- **Done when:** a reader who reads only these two lines can say what the PR does and why it was needed.

### 3. Executive summary — at CUSTOMER / product altitude
- **What:** the impact stated for a **paying customer / end user** in plain language: what breaks for them, and why it matters (trust, safety, money, data-exposure, compliance) — *not* the code mechanism.
- **The question it answers:** "If I never open the diff, why do I care?"
- **Done when:** a non-engineer stakeholder (PM, support, exec) understands the stakes.

### 4. Two visuals at two altitudes
- **4a. Product/UX-impact visual** — the *harm* (or *benefit*) as the **customer experiences it**: the journey of the bug damaging (or the fix protecting) a real user. Before-vs-after.
- **4b. Technical-mechanism visual** — the *code/data flow* of the bug vs the fix.
- **What:** rendered diagrams (Mermaid) or tight before/after tables. Color the bad path red, the good path green.
- **Done when:** a customer-side reader gets 4a; an engineer gets 4b; neither has to read prose to grasp the shape.

### 5. Reviewer decision box
- **What:** a compact table a reviewer scans to decide: **Risk · Blast radius · Behavior change (intended) · Verified ✅ · NOT verified ⚠️ · Where to look first.**
- **Done when:** a reviewer can make a merge/no-merge call from this box alone, and knows the riskiest lines to inspect.

### 6. Evidence / KPI table
- **What:** verification quantified at a glance — build status, tests (total/pass/fail/skip), review passes — **including an honest "failures/impact attributable to THIS PR" cut** that separates your change from pre-existing noise.
- **Done when:** the numbers are *observed* (you ran it), and the "caused by this PR" line is explicit.
- **When the KPI shows pre-existing failures you're attributing *away* from this PR**, include a **per-failure attribution appendix** (near the end): each failure labeled — `stale-test` / `pre-existing-defect` / `real-bug` / `caused-by-this-PR` — with the *evidence* that assigns the label (the symbol that changed + the failing code's last-touching commit vs this PR's SHAs). A bare "N are pre-existing" without the per-item proof is an assertion, not evidence — see #589's "Detailed test analysis." Omit the appendix when there is no pre-existing noise to separate out (per Smallest Structure).

### 7. Context map — durable references
- **What:** the pointers a future reader/agent needs to navigate: **key files** (repo-relative paths, agent-openable), **the convention this PR establishes** (what future code should now do), **commits by role**, **related issues**, and **follow-ups**.
- **Done when:** someone landing here in 6 months can find the primitive, copy the pattern, and knows what was deliberately left for later.

### 8. Deep detail (progressive) + radical honesty
- **What:** root cause, approach (and the roads *not* taken + why), scope, review findings (including any regression the change *itself* introduced and how it was caught), testing, and out-of-scope/follow-ups.
- **Honesty requirements (non-negotiable):** state what is **NOT** verified; label **intended** behavior changes as intended; **disclose self-introduced issues** found and fixed; **never claim a green you didn't observe**.
- **Done when:** the deep reader trusts the PR *because* it volunteers its own limits, not despite hiding them.

### 9. Resolve automated review before "done"
- **What:** a PR isn't finished when the description is written — it's finished when the automated review (Copilot / bots) is **resolved**. Fetch every automated-reviewer comment and **validate each against the actual code** — neither rubber-stamp nor reflexively dismiss (judge in context, trace the call graph). **Integrate** the genuine ones; **dismiss** the false positives *with the evidence* that proves them false; reply on each thread so the resolution is visible, and mark it resolved.
- **Reflect it in the description** — in the decision box's *Verified* line or a short "Automated review — addressed" note (e.g. "automated review: 2 integrated, 1 dismissed-with-reason").
- **Watch for the widened bug:** an automated reviewer flags an *instance*, not always the *class*. When you confirm one is real, sweep for the same pattern elsewhere before fixing — a single flagged line turned out to be a 5-site bug in #589. Fix the class, not just the line.
- **Done when:** every automated-review thread is either integrated (with the commit) or dismissed (with proof), none left dangling, and the description records the outcome.

---

## The generative questions (fill each section by answering these)

| Section | The question the author answers |
|---|---|
| Executive summary | "How does this hurt/help a paying customer, in one paragraph?" |
| Product visual (4a) | "Draw the moment the user is harmed (or protected). Before vs after." |
| Technical visual (4b) | "Draw the code path that fails vs the one that now works." |
| Decision box | "What does a reviewer need to say yes/no and know what to inspect?" |
| KPI table | "What did I actually run, and what of the result is *mine* vs pre-existing?" |
| Context map | "If an agent had to extend this in 6 months, what paths + convention + follow-ups do they need?" |
| Deep detail | "What would a skeptical senior engineer challenge — and what am I hiding?" |
| Resolve automated review | "Is every Copilot/bot comment integrated (real) or dismissed with proof (false), none dangling — and did I check whether a flagged *line* is actually a *class*?" |

---

## Enforcement

- **`.github/pull_request_template.md`** — a skeleton with these headings, so every new PR starts pre-shaped (humans fill it in; GitHub auto-populates it).
- **CLAUDE.md / agent instructions** — a rule that Claude-authored PRs must satisfy this standard, so agent-generated descriptions carry it by default.
- **The test for a good PR description:** would a reviewer merge from the first screen, and could a future agent navigate the change from the bottom? If either fails, it's not done.
