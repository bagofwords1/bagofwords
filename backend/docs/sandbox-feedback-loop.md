# Per-hunk tracked-changes — sandbox feedback loop

This documents the **expected behavior** of per-hunk accept/reject on the
immutable-build ("cherry-pick") model, and the **DB-tracing reproduction loop**
used to verify it before touching the UI.

## Model (the contract)

- **Builds are immutable snapshots.** A build is never mutated after creation.
  `is_main` is a pointer to the live build. Evals pin a `build_id`, so they are
  fully reproducible.
- A **suggestion** is an immutable non-main build (`status in draft|pending_approval`,
  `source in user|ai|git`) that proposes a new version of one or more instructions.
- The review of an instruction is `diff(main_text, proposed_text)` per suggestion.
  The unit of review is a **hunk** (a contiguous run of word-level changes).
- **Accept hunk** → create a *new* build = `main + that hunk`, promote it to main.
  The proposed build is untouched. Because main now contains the hunk, it no
  longer appears in `diff(main, proposed)` on the next read — accepts need no
  per-hunk state.
- **Reject hunk** → append the hunk's stable content key to
  `InstructionBuild.rejected_hunks` on the proposed build. Rejected hunks are
  filtered out of the diff. Nothing else is mutated.
- A suggestion is **resolved** when `diff(main, proposed)` minus rejected is empty
  (its instruction is removed from the build / it stops surfacing).

### Stable hunk key

`key = sha1(before_text \x00 after_text \x00 left_context[-24:])`. Content-based,
so it survives `main` advancing as sibling hunks are accepted. Used only for
*reject* persistence; accept references the live hunk index + a `main_version_id`
concurrency token (mismatch ⇒ client refetches).

## Expected behavior — invariants the repro asserts

1. **Immutability:** accepting/rejecting never changes an existing build's
   `BuildContent` versions. Accept creates a brand-new build; the proposed build's
   rows are byte-identical before and after.
2. **Accept advances main:** after accepting hunk H, the new main text =
   `apply(old_main, [H])`; a new build row exists with `is_main=true`, the old
   main has `is_main=false`.
3. **Accepted hunk drops out:** re-reading the same suggestion no longer lists H.
4. **Reject keeps siblings:** rejecting hunk H removes only H from the diff; the
   other hunks of the same build still surface; `rejected_hunks` contains H's key.
5. **Reject one ≠ reject all:** a 3-hunk build, reject 1 → 2 remain.
6. **No re-add:** accepting a deletion hunk does not later re-introduce the text;
   accepting an addition does not later propose its removal.
7. **Siblings de-dupe via main:** two builds proposing the same hunk — accepting
   it in build A makes it vanish from build B's diff (main now has it).
8. **Resolved when empty:** once every hunk of a build is accepted or rejected,
   the build no longer surfaces for that instruction.
9. **Eval reproducibility:** the proposed `build_id` and each promoted `build_id`
   remain valid, immutable snapshots after the whole sequence.

## Complex scenario the repro drives

Instruction `SALES_OVERVIEW` (multi-paragraph). Starting main = v0.

1. Create **suggestion A** (user): 3 separate edits — prepend a line, change a
   word mid-text, delete a trailing line. (3 hunks)
2. Create **suggestion B** (ai): overlaps A (also prepends the same line) + adds a
   new trailing line. (cumulative-superset sibling)
3. Create **suggestion C** (ai): edits a different instruction in the same build
   (multi-instruction build) + one edit to SALES_OVERVIEW.
4. **Accept** hunk 2 of A (the mid-text word change). → main advances; assert the
   word change is live, the other A hunks remain, B/C unaffected except where they
   shared.
5. **Reject** hunk 1 of A (the prepend). → assert it's gone from A's diff,
   `rejected_hunks` has it, B's prepend (same content) handled per its own diff.
6. **Accept** the shared prepend via B. → assert it lands once; A's prepend (if not
   rejected) drops out of A too (main now has it).
7. Make a **new edit** (suggestion D) on top of the now-advanced main.
8. Accept the remaining hunks; assert all suggestions resolve and main = expected
   final text. Assert every historical build_id still resolves to its snapshot.

## Running the loop

```
cd backend
set -a && . ./.env && set +a            # sqlite at db/app.db (or a throwaway copy)
PYTHONPATH=. ./.venv/bin/python scripts/repro_hunks.py
```

`scripts/repro_hunks.py` builds the scenario through the service layer (no HTTP),
and after **every** action prints a DB trace: builds (id, #, is_main, status,
source, base), per-build `BuildContent` (instruction → version → text head),
`rejected_hunks`, and the instruction's current/main text. It asserts the
invariants above and exits non-zero on the first violation.

The DB is traced directly with `sqlite3` (no ORM) so the trace reflects exactly
what is persisted.
