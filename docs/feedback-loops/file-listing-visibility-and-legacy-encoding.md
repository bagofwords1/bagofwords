# Feedback Loop — file listings the model can use: observation inventory + legacy filename recovery

Observed live on a Hebrew mortgage-files directory, two compounding defects:

1. **list_files/search_files observations carried only a count** ("Listed 24
   file(s)") — names/ids existed only in the UI-facing output, so the model
   re-listed with identical parameters until the repeated-call circuit
   breaker ended the turn with a fabricated *"Task completed successfully…
   executed 2 times with the same parameters"* (`agent_v2` breaker). Same
   root cause as the read_file/grep_files fixes (#666/#670): the planner
   consumes the observation, never the output.
2. **Legacy-encoded filenames degraded to `?????`** — names stored in cp1255
   (Windows-Hebrew shares, zips extracted without a codepage) reach Python as
   surrogateescape strings; the persistence sanitizer (0.0.458) correctly
   refuses the lone surrogates, so every Hebrew character became `?` —
   unreadable AND un-round-trippable. (Pre-0.0.458 these names hard-500'd the
   report; the sanitizer traded the crash for data loss.)

## The fix

- `list_files` observation gains a bounded inventory (`name — path — size
  [id=…]`, 50 rows, "+K more — narrow with name_pattern"); `search_files`
  likewise (30 hit rows). History compaction extends to both (superseded
  listings collapse to a length marker).
- The repeated-call breaker message (`repeated_call_final_answer`) no longer
  claims success — it says the call was repeated, points at the existing
  result, and instructs to use it or change parameters.
- Legacy filename recovery in `_file_source_common`: `recover_filename`
  (surrogates → cp1255/cp1252 best-effort decode; replace as last resort)
  applied at `network_dir`'s `_rel_id`/`_entry`, and `legacy_fs_candidates`
  in `_resolve` maps a recovered id back to the on-disk byte form — so
  listing, read_file, grep, and file_version all round-trip. Include-globs
  match against the recovered (human-written) form.

## Loop A — deterministic

`backend/tests/unit/test_file_listing_visibility_and_encoding.py` (9 tests):
fixture creates a REAL cp1255-byte filename via a bytes path. Recovery
(listing shows the true Hebrew name, JSON-safe, no `??`), id round-trip
through read/grep/file_version, clean names unaffected, observations carry
names+ids for both tools, compaction coverage, breaker honesty.
Pre-fix on main (`8a065434`): **8 failed / 1 passed**. Post-fix: **9 passed**;
regression suites 123 passed (known pre-existing `test_file_tools` resolver
failure only).

## Loop B — live (Anthropic API, full stack)

Recreated the reported scenario: numeric-id JSONs + a cp1255-named PDF in a
`network_dir` connection, asked in Hebrew "מה יש ב-jsons? תן לי את שמות הקבצים":

- **ONE `list_files` call** (previously two identical + breaker), all three
  file names in the answer, no "identical parameters" text.
- Follow-up "what's the Hebrew PDF called?" → answer contains
  **דוח משכנתא 2024.pdf** — recovered, zero `?????`.

## Regression notes

Recovery only activates for surrogate-carrying names — clean UTF-8 names are
byte-identical pass-throughs (asserted). The resolve fallback only runs when
the verbatim path doesn't exist and stays root-confined + glob-checked on the
real path. Residual: two distinct byte-names that recover to the same display
string collide (first match wins) — log-worthy rarity, acceptable v1.
