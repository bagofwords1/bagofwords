---
name: release-notes
description: Bump the version and write a CHANGELOG.md release-notes entry after a user-facing change ships. Use when asked to "bump the version", "cut a release", "add a changelog entry", or "write release notes". Produces one minimal, user-facing, technical bullet per change.
---

# Release notes & version bump

Two files, one commit. `VERSION` drives the release; `CHANGELOG.md` is the
human-readable history.

- **`VERSION`** — a single line, `MAJOR.MINOR.PATCH` (e.g. `0.0.448`). The
  Release workflow (`.github/workflows/release.yml`) reads it, tags `vX.Y.Z`,
  and extracts the matching `CHANGELOG.md` section as the GitHub release body.
- **`CHANGELOG.md`** — newest version first. Served to users at `/changelog`
  (parsed by `backend/app/routes/changelog.py`), so the format is load-bearing.

## Steps

1. **Bump `VERSION`.** Increment the **patch** by one (`0.0.448` → `0.0.449`)
   unless told otherwise. No leading spaces, no trailing newline changes —
   just the number.
2. **Add a section at the top of `CHANGELOG.md`**, directly under
   `# Release Notes`:
   ```
   ## Version <new-version> (<Month DD, YYYY>)
   - <One short line stating the change> (#<PR>)
   ```
   Use today's date, format `July 12, 2026`. One bullet per user-facing
   change; group the whole release under this single version heading.
3. **Commit both files together**: `Bump version to <X.Y.Z> with changelog entry`.

## How to write a bullet

**One short plain line per change. That's the whole note.**

```
- Added Google Chat as a new chat channel (#771)
```

- Start with the verb: `Added …`, `Fixed …`, `Renamed …`, `Removed …`.
- Name the feature the way a user would say it, then stop. No architecture,
  no implementation, no docs pointers, no marketing.
- **`(#PR)`** — the pull request number, if known. Omit if there's no PR.
- Only append an ` — detail` clause when the user needs a concrete knob to
  use the change (a setting name, env var, or changed default). One clause,
  not a sentence.

### Rules

- **Minimal.** If a bullet wraps past one line, cut it down.
- **User-facing only.** Skip pure refactors, test-only changes, CI, and
  dependency bumps. If a user can't observe it, it's not a release note.
- **Be specific, not vague.** "Fixed SSO login for mismatched email casing"
  beats "Improved login reliability".
- **No filler.** Drop "now", "the ability to", "support for", "improved",
  "various", "enhancements".
- **Never real data.** No customer/org names, account identifiers, emails,
  tokens, connection strings, or any PII — release notes are public. Use
  generic placeholders (`<name>`, `acme`) if an example needs one.

## Examples

Feature:
```
- Added Google Chat as a new chat channel (#771)
```

Feature with a knob (the only case that earns a detail clause):
```
- Added concurrent multi-tool execution (#598) — controlled by the `ai_tool_concurrency` org setting (default 4)
```

Fix:
```
- Fixed iOS focus-zoom on the report prompt box (#600)
```

Config/model change (no PR):
```
- Updated OpenAI model presets — GPT-5.6 Terra is the new default
```

A full release section:
```
## Version 0.0.438 (July 7, 2026)
- Added Triggers: webhooks that spawn agent sessions, under a new Automations page (#562)
- Added per-file QVD indexing progress with stop, size, and duration (#564)
- Fixed WhatsApp delivery of agent replies and verification-page branding (#565)
```
