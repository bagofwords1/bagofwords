# Feedback Loop — "instruction with pending changes but no changes at all"

An instruction shows the amber **"Pending review"** dot/label in the /agents
Knowledge Explorer (and is counted in the "N pending" badge), but opening it
reveals no tracked changes to review — the diff pane is empty. This validates
that the "Pending review" signal and the tracked-changes review are two
computations of the same fact that could disagree, and that a suggestion whose
only change is a no-op against current main triggers the mismatch.

## Root cause (validated)

The "Pending review" signal and the diff come from two paths:

- **Badge / status label** — `KnowledgeExplorer.vue:548-549` (`isPending`) reads
  `GET /instructions/pending-changes` →
  `InstructionService.get_pending_change_instruction_ids`
  (`instruction_service.py:1485`) → `has_live_hunk_against_main`
  (`text_hunks.py:279`).
- **The diff pane** — `GET /instructions/{id}/review-hunks`
  (`InstructionService.review_hunks`, `instruction_service.py:1462-1465`, which
  drops a suggestion when `not shown`) → `rebased_hunks_against_main`
  (`text_hunks.py:208`).

`has_live_hunk_against_main` had an eager shortcut (`text_hunks.py:296-297`
before the fix):

```python
if proposed == base or proposed == main:
    return False
if base == main and not rejected:
    return True          # <-- declares pending WITHOUT computing a hunk
```

That `return True` flags the instruction pending whenever the suggestion forked
from current main and its text differs — **without** running the no-op skips that
`rebased_hunks_against_main` applies:

- already-applied region (`text_hunks.py:264`),
- **idempotent insertion** — a token already present at the anchor
  (`text_hunks.py:266-269`),
- no net change against main (`text_hunks.py:272-273`).

So a suggestion that, e.g., re-inserts a word already adjacent in the text
(`"alpha beta gamma"` → `"alpha beta beta gamma"`) is a no-op: it yields **zero**
review hunks, yet the badge fired. Result: "Pending review" with nothing to
review.

## Loop A — deterministic reproduction (no external services)

Sandbox setup (`backend/`, Python 3.12):

```bash
cd backend
pip install uv && uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" && mkdir -p db
```

Function-level probe — the badge says pending, the diff renderer finds nothing:

```python
from app.services.text_hunks import has_live_hunk_against_main, rebased_hunks_against_main
base = main = "alpha beta gamma"; proposed = "alpha beta beta gamma"
print(has_live_hunk_against_main(base, proposed, main))   # True  (badge)
print(len(rebased_hunks_against_main(base, proposed, main)))  # 0  (review pane)
```

End-to-end, through the two endpoints that disagree
(`tests/e2e/test_instruction.py::test_pending_badge_requires_a_live_review_hunk`,
parametrized over three anchors so it asserts the invariant, not one string):

```bash
TESTING=true uv run pytest tests/e2e/test_instruction.py \
  -k test_pending_badge_requires_a_live_review_hunk -m e2e --db=sqlite -q
```

Observed **FAIL** on unfixed code (all three cases):

```
AssertionError: instruction is flagged 'Pending review' by /pending-changes but
/review-hunks renders 0 hunks — pending with nothing to review
assert 0 > 0
3 failed
```

The invariant asserted: *an instruction flagged pending by
`/instructions/pending-changes` must have at least one live hunk in
`/review-hunks`.*

## The fix

`backend/app/services/text_hunks.py`, `has_live_hunk_against_main` — remove the
eager `if base == main and not rejected: return True` shortcut so the predicate
agrees with the authoritative hunk computation. A suggestion that changes
nothing against main no longer counts as pending; healthy suggestions still
yield ≥1 hunk and stay pending.

Re-run Loop A after the fix:

```
8 passed, 18 deselected      # 3 new repro cases + existing pending/consistency tests
```

Full instruction suites (regression sweep):

```
tests/e2e/test_instruction.py tests/e2e/test_instruction_evidence.py
tests/e2e/test_instruction_resolve.py  ->  31 passed
```

## Performance (why the fix is not the naive one)

Dropping the shortcut naively — always falling through to
`rebased_hunks_against_main` — is **correct but slow**. `compute_hunks` runs a
`difflib` word diff whose cost is ~O(n²) on real prose (every whitespace and
punctuation token is identical, difflib's worst case), and the /agents pending
sweep (`get_pending_change_instruction_ids`, `instruction_service.py:1485`) runs
this predicate for *every* pending suggestion on page load. Measured on a
realistic ~2.4 KB instruction with a one-word edit:

| implementation | µs / suggestion | correct? |
|---|---:|---|
| original (buggy shortcut) | 0.3 | no — flags no-op as pending |
| fix v1 (diff always) | 32,458 | yes |
| **fix v2 (fast-path, shipped)** | **437** | yes |

So the shipped fix keeps an **O(n) fast-path** for the common case (a suggestion
forked from current main, no rejects): any deleted main token, or any inserted
token absent from main, is unambiguously a real reviewable hunk — decided by two
`Counter`s over the token streams, no diff. Only an *all-duplicate-token*
insertion is ambiguous (the potential no-op) and falls through to the
authoritative diff — and that text is small by nature. On large instructions the
fast-path is 40–215× faster than the naive fix (e.g. ~7 KB: 285 ms → 1.3 ms) and
stays byte-for-byte consistent with the review pane.

Correctness of the fast-path is pinned by a fuzz test cross-checking it against
`rebased_hunks_against_main` over 10k+ random edits (including the repeated-token
boundary cases), covering both `base == main` and drifted `base != main`:
`tests/unit/test_text_hunks_pending.py` — `28 passed`.

## What this proves / regression notes

- The badge and the tracked-changes review now derive "pending" from the exact
  same rule, so an instruction can no longer sit on "Pending review" with an
  empty diff.
- The e2e test is the surviving regression guard and asserts the general
  invariant (badge ⟹ ≥1 live hunk) across three distinct anchors, not the single
  reported string.
- No frontend change was needed: the inline diff/banner
  (`KnowledgeExplorer.vue:672-674`, `pendingViews`) already required effective
  hunks, so once the backend stops flagging the no-op, the label clears with it.
