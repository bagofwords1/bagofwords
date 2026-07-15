# Feedback Loop — unified read_file/grep_files over conversation attachments, images to vision, page_range × vision

Files attached to the conversation (uploads, attach_file results) had NO
reader: `resolve_file_client` only walks file connections, so an agent with
uploaded JSONs/PDFs could see the `<files>` index but never lazily read the
content — and an image from an earlier turn was architecturally unreachable
(eager vision covers only the current completion,
`agent_v2._load_images_as_input`). The `<files>` index itself rendered
json/text/images as "unsupported" (name-only), so the planner couldn't even
decide what to read. Scanned PDFs additionally ignored `page_range` in the
vision path (always pages 1–8).

## The change

One resolution ladder in `read_file`/`grep_files`: session file (report.files
= the allow-list, org-checked) when no connection is named; connector
otherwise (explicit beats implicit). `SessionFileClient` delegates to
NetworkDirClient rooted at the file's directory, so session files get the
identical read semantics (windowed reads, page_range, tabular parsing,
vision fallback) with no second pipeline. Plus:

- session reads echo the existing file id — no duplicate File rows per read
- `page_range` × vision: textless pages are rasterized (`render_pdf_pages_images`)
  for the REQUESTED range, not just 1–8
- catalog gating: `capabilities_for_report_files` — read_file/grep_files
  appear when the report has attached files, connector or not
- `<files>` index previews: json/text head (500 chars + size), image
  dimensions, PDF "N of M pages previewed — use page_range"
- history: user messages carry `[attached: name (type, id=…)]` lines
  (batched `report_file_association.completion_id` lookup) and read digests
  record `pages X of Y` / "viewed by vision — re-view with read_file"

## Loop A — deterministic

`backend/tests/unit/test_read_file_session_files.py` (20 tests): resolution +
allow-list denials (other report's file, org mismatch, explicit connection
never falls back to session), windowed/page_range/json reads over session
files with observation content, no re-attach on re-read, image→vision blocks
(+ gates off without vision / allow_llm_see_data), scanned-PDF page_range
rasterizing the RIGHT page (distinct MediaBox sizes identify pages), session
grep sweep with binary skip, catalog gating, previews, history digests.

Pre-fix on main (`f4d91331`): 9 failed (capability gaps), 4 passed
(deny-guardrails). Post-fix: **20 passed**, plus the full affected suite
(87 passed; the known pre-existing `test_file_tools` resolver failure
reproduces on clean main).

## Loop B — live (Anthropic API, Claude Haiku vision), full stack + UI

All five scenarios pass; screenshots in `assets/session-files/`:

- **S1 session JSON** — upload → "what's the verification_code" → exact code;
  trace: `read_file({file_id})`, no connection. `S1-session-json.png`
- **S2 image re-access on a LATER turn** (the killer test) — turn 1 uploads
  badge.png + "just say hello"; turn 2 "what's the access code in the image I
  attached earlier" → `read_file` on the image → vision → **RED-HORSE-42**
  verbatim. Previously architecturally impossible. `S2-image-reaccess.png`
- **S3 session PDF page_range** — `read_file({file_id, page_range: '2'})` →
  page-2 code. `S3-pdf-page-range.png`
- **S4 scanned PDF page via vision** — image-only PDF, page 2 requested →
  rasterized → vision reads the drawn code; UI shows the `pages 2-2 / 3`
  badge. `S4-scanned-pdf-vision.png`
- **S5 grep over uploads** — `grep_files({pattern})` with no connection sweeps
  the uploaded logs, quotes the exact line. `S5-grep-uploads.png`

## Regression notes

Deny-direction tests (foreign report's file, org mismatch, explicit
connection_id never reading session data) passed BEFORE the change and must
keep passing — they are the security contract of the new resolution source.
Fixture note: vision-read codes must be drawn legibly (large font) and avoid
0/O/Q lookalikes — S2 initially "failed" on the model reading Q as 0.
