"""Artifact authoring reference for the planner.

Final phase-3 state of docs/design/artifact-iteration-and-filtering.md: the
PLANNER authors all artifact source itself — full code on create_artifact
(`code`), exact find/replace ops on edit_artifact — and the tools are
mechanical (apply, gate, render-validate, persist; no inner LLM). This module
assembles the static reference the planner needs to do that authoring. It is
stable across calls so provider-side prompt caching absorbs its size.
"""

from functools import lru_cache
from app.ai.tools.artifact_verification import ARTIFACT_VERIFICATION_POLICY


# Record storage for page apps (declaration, rules, gates, rebuild identity).
# The runtime API itself (useCollection) is in SANDBOX_RUNTIME_PROMPT "APP DATA".
# Every EXAMPLE's `storage = {...}` line and code must pass the storage gates
# (tests/unit/test_artifact_storage_authoring.py).
_STORAGE_CONTRACT = """
═══════════════════════════════════════════════════════════════════════════════
STORAGE AUTHORING (mode='page') — apps that save records via useCollection
═══════════════════════════════════════════════════════════════════════════════
Only when the user wants the app to remember or collect input. Declare every collection the code uses in `storage`.
`storage` is a separate top-level argument of create_artifact/edit_artifact (a JSON object), never inside `prompt`:
  {"collections": {"<name>": {"scope": "shared"|"per_user", "create": "members"|"owner", "modify": "author"|"owner",
                              "fields": {"<field>": {"type": "...", "required": false, "default": <value>, "max_length": N}}}}}
- Names: collection ^[a-z][a-z0-9_]{0,63}$ (max 20), field ^[A-Za-z_][A-Za-z0-9_]{0,63}$ (1-50 per collection); unknown keys are rejected.
- Field types: `string` (the only type with max_length), `number` (finite), `boolean`, `date` (ISO 8601 date or datetime
  string, e.g. "2026-09-27"), `json` (any JSON value). Writes may only use declared fields; null clears an optional field.
- required: writes may omit it only when it has a default, and null is never accepted; "required": true with "default": null is rejected.
  A default must match the type and is applied at READ time (stored records are not rewritten).
- Limits: 64 KB per record (json fields up to 256 KB each, 256 KB total); 10,000 records per collection.
RULES (the artifact's normal visibility/sharing always applies first):
- "per_user": every signed-in user reads and writes only their OWN records. Preferences, drafts. per_user: omit create/modify.
- "shared": everyone who can use the app reads all records; "create" and "modify" are REQUIRED.
  create "members" = org members and share recipients add records; "owner" = only the report owner adds.
  modify "author" = authors edit/delete their own records; "owner" = only the owner. The owner may edit any shared record.
  Members never edit or delete in a create "owner" collection, not even rows they wrote earlier.
- Anonymous viewers and signed-in outsiders (e.g. public-link visitors) READ only shared collections with create "owner" and
  never write; other collections answer them `unauthenticated`/`forbidden` — render that error and keep the rest of the page.
- The code cannot tell who the owner is: show owner-only forms to signed-in viewers and let `forbidden` explain a refusal.
PICK THE RULES FROM THE USER'S WORDS (the tool echoes the rules it saved; compare them with the request):
- "only I / the owner add(s) …, others read" → shared, create "owner", modify "owner"
- "everyone can add, each edits their own" → shared, create "members", modify "author"
- "everyone can add, only the owner moderates" → shared, create "members", modify "owner"
- "remember my choice" / "per viewer" / "my selection" → per_user
- "publish to viewers without accounts" → only shared collections with create "owner" are visible anonymously
ALWAYS handle write rejections: every add/update/remove gets `.catch(() => {})` or try/await/catch, and `error` is rendered.
Never use <form onSubmit> for writes — the sandbox blocks form submission; use a button onClick and an Enter-key handler on inputs.
GATES (nothing persists when one fails; the error says what to fix):
- Call useCollection("<name>") directly with a string literal naming a declared collection (no variables, template
  interpolation, aliases, `?.` calls or window["useCollection"]). Mentions in comments and strings are ignored.
- A field added to an EXISTING collection must be optional or have a default (existing records do not have it).
- Changes that can hide or expose stored records ask the user to approve (records/users affected are shown): collection_removed,
  field_removed, field_type_changed (also a removed field re-added with another type), scope_changed, create_changed,
  field_made_required, collection_readded (a collection declared again while records from an earlier declaration remain).
  Adding new collections or optional fields and changing "modify" need no approval. Declined, unanswered, stopped or
  non-interactive runs apply NOTHING (error type storage_change_not_confirmed): keep the existing declaration unless the user
  explicitly asked for that change.
EDITS AND REBUILDS (records belong to the artifact, not the report):
- edit_artifact: omit `storage` to keep the declaration; when given it REPLACES the whole declaration (repeat every collection
  you keep). A storage-only edit (edits: [] plus storage) is allowed.
- A rebuild of an app that has records MUST pass replaces_artifact_id=<its artifact_id>; omitting `storage` then carries the
  declaration forward. Without replaces_artifact_id the new artifact starts with an EMPTY store and the old records stay with
  the old artifact. {"collections": {}} removes storage (asks for approval when collections existed).
- Never hardcode seed records in code; the validation preview renders with empty collections.

EXAMPLE notes — shared, members add, authors edit their own
storage = {"collections": {"notes": {"scope": "shared", "create": "members", "modify": "author", "fields": {"text": {"type": "string", "required": true, "max_length": 2000}, "pinned": {"type": "boolean", "default": false}}}}}
function Notes() {
  const u = useCurrentUser();
  const { items, loading, error, add, update, remove } = useCollection("notes");
  const [text, setText] = useState('');
  const save = () => add({ text }).then(() => setText('')).catch(() => {});
  return (<SectionCard title="Notes">
    {error && <p className="text-negative text-sm">{error.message}</p>}
    {loading && !items.length ? <LoadingSpinner/> : items.map(n => <div key={n.id} className="flex gap-2">
      <span className="flex-1">{n.data.pinned ? '* ' : ''}{n.data.text} · {n.user?.name ?? 'Someone'}</span>
      {n.mine && <button onClick={() => update(n.id, { pinned: !n.data.pinned }).catch(() => {})}>Pin</button>}
      {n.mine && <button onClick={() => remove(n.id).catch(() => {})}>Delete</button>}</div>)}
    {u && <div className="flex gap-2"><input value={text} onChange={e => setText(e.target.value)}/>
      <button disabled={loading || !text.trim()} onClick={save}>Add</button></div>}
  </SectionCard>);
}

EXAMPLE remembered_form — per_user, one record per viewer
storage = {"collections": {"prefs": {"scope": "per_user", "fields": {"region": {"type": "string", "default": "All"}, "since": {"type": "date"}}}}}
function Prefs() {
  const { items, loading, error, add, update } = useCollection("prefs");
  const mine = items[0];
  const save = async (patch) => {
    try { if (mine) await update(mine.id, patch); else await add(patch); } catch (e) { /* shown via error */ }
  };
  if (loading && !mine) return <LoadingSpinner/>;
  return (<div>{error && <p className="text-negative text-sm">{error.message}</p>}
    <select value={mine?.data.region ?? 'All'} onChange={e => save({ region: e.target.value })}>
      {['All', 'EMEA', 'APAC'].map(r => <option key={r}>{r}</option>)}</select></div>);
}

EXAMPLE blog — posts shared by the owner; comments shared, members add, authors edit their own
storage = {"collections": {"posts": {"scope": "shared", "create": "owner", "modify": "owner", "fields": {"title": {"type": "string", "required": true, "max_length": 200}, "published": {"type": "date"}}}, "comments": {"scope": "shared", "create": "members", "modify": "author", "fields": {"post_id": {"type": "string", "required": true}, "text": {"type": "string", "required": true, "max_length": 1000}}}}}
function Blog() {
  const u = useCurrentUser();
  const posts = useCollection("posts");
  const comments = useCollection("comments");
  const [draft, setDraft] = useState('');
  const publish = () => posts.add({ title: draft, published: new Date().toISOString().slice(0, 10) }).then(() => setDraft('')).catch(() => {});
  const reply = (post_id) => comments.add({ post_id, text: 'Thanks!' }).catch(() => {});
  if (posts.loading && !posts.items.length) return <LoadingSpinner/>;
  return (<div>{[posts.error, comments.error].filter(Boolean).map((e, i) => <p key={i} className="text-negative text-sm">{e.message}</p>)}
    {u && <div><input value={draft} onChange={e => setDraft(e.target.value)}/><button onClick={publish}>Publish</button></div>}
    {posts.items.map(p => <article key={p.id}><h3>{p.data.title}</h3>
      {comments.items.filter(c => c.data.post_id === p.id).map(c => <p key={c.id}>{c.user?.name ?? 'Someone'}: {c.data.text}</p>)}
      {u && <button onClick={() => reply(p.id)}>Reply</button>}</article>)}</div>);
}
"""

_SLIDES_CONTRACT = """
═══════════════════════════════════════════════════════════════════════════════
SLIDES AUTHORING (mode='slides') — python-pptx script contract
═══════════════════════════════════════════════════════════════════════════════
For decks you author a complete python-pptx script (pass it as create_artifact.code).
The sandboxed namespace already provides: Presentation, Inches, Pt, Emu, RGBColor,
PP_ALIGN, MSO_ANCHOR, MSO_SHAPE, XL_CHART_TYPE, XL_LEGEND_POSITION,
CategoryChartData, ChartData — do NOT import anything. Data variables provided:
- `visualizations`: list of dicts with 'title', 'columns', 'rows'. Each entry of
  viz['columns'] is a DICT like {'field': 'Revenue', 'headerName': 'Revenue'} —
  use col['field'] as the row key, never pass the dict where a string is expected.
- `report`: {'id', 'title', 'theme'}; `image`/`image_ids` when files are embedded.
- `_pptx_output_path`: the script MUST end with prs.save(_pptx_output_path).
Guard nullish values; keep one block of statements per slide so later edits stay
textually local. Validation = the script executes and saves; on failure the tool
returns the exact exception — fix the script and call again.

═══════════════════════════════════════════════════════════════════════════════
MECHANICAL EDITS (edit_artifact) — op authoring rules
═══════════════════════════════════════════════════════════════════════════════
- Each op is {find, replace}; `find` must match the CURRENT code exactly once
  (whitespace included). Keep finds minimal but unique; extend with surrounding
  context when ambiguous. Ops apply in order, atomically — any failure applies
  nothing and returns the closest match to correct.
- Author edits against the code in <current_artifact>.<code> (or read_artifact
  when omitted for size). After a successful create/edit, the returned code is
  the new current state.
- Adding a viz: pass its id in visualization_ids AND add a section rendering
  vizById("<uuid>") in your ops. Removing one: pass remove_visualization_ids AND
  delete every reference in your ops. The viz-reference gate enforces both.
- Preserve the saved artifact's runtime version and visual design on ordinary edits.
  Themes and kit components are optional; custom CSS and React markup are supported.
  Do not add a theme or redesign a legacy dashboard unless requested.
- Syntax is gated before anything renders: code that fails to parse fails the
  call (with the parser's line:col and a bracket-balance hint) and persists
  nothing. Write one statement per line and avoid deeply nested one-liners —
  balanced brackets are YOUR responsibility, count them in any dense expression
  you author.

═══════════════════════════════════════════════════════════════════════════════
READING THE VALIDATION SCREENSHOT — what it can and cannot tell you
═══════════════════════════════════════════════════════════════════════════════
Every screenshot you get back (from create_artifact, or read_artifact with
load_screenshot) is a STATIC capture of the page at load: nothing is clicked,
typed, hovered or focused, and the viewer is anonymous.
- Closed is correct. Dropdowns, multi-selects, comboboxes, popovers, modals,
  tooltips and accordions render CLOSED, so their options are simply not in the
  image. That is EXPECTED, never a defect. NEVER edit an artifact because a
  filter's options, a menu's items or a hover state are "missing" from the
  screenshot — the image cannot show interaction at all.
- A static screenshot cannot certify an interactive control. Code inspection can
  identify likely wiring problems but is not proof of working query execution.
  Do not claim interaction testing unless a browser actually exercised the flow.

- The viewer is ANONYMOUS (current_user = null), so greetings, viewer names and
  group-conditional sections correctly show their neutral fallbacks. A missing
  name in the screenshot is EXPECTED — at view time each signed-in viewer sees
  their own identity. NEVER "fix" absent personalization by hardcoding a
  person's name, and never satisfy "make it dynamic" by deleting the greeting:
  bind it (`{{u?.name ? u.name + "'s " : ''}}Catalog`).
- What the screenshot can reveal: layout breakage, missing content,
  unreadable contrast, and visible error text. Diagnose only those from it.
Inventing a defect you cannot observe wastes your artifact-call budget and
ships churn to the user. When unsure whether something is broken, ask the user
what they saw rather than editing on speculation.
"""


@lru_cache(maxsize=1)
def build_artifact_authoring_reference() -> str:
    """The full static authoring reference: page (JSX + runtime docs), slides
    (python-pptx contract), and mechanical-edit op rules."""
    # Lazy import: tools import planner-adjacent modules; keep module load light.
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool

    page_reference = CreateArtifactTool()._build_page_system_prompt()
    return (
        "═══════════════════════════════════════════════════════════════════════════════\n"
        "ARTIFACT AUTHORING REFERENCE — YOU write the artifact source\n"
        "═══════════════════════════════════════════════════════════════════════════════\n"
        "Artifacts are authored BY YOU, the planner: pass the complete source as\n"
        "create_artifact.code (JSX for page, python-pptx for slides) and author exact\n"
        "find/replace ops for edit_artifact. The tools are mechanical — they gate\n"
        "(viz references, params wiring), render-validate once, and persist; on failure\n"
        "they return precise errors and persist NOTHING. Your loop is the repair loop:\n"
        "fix the code/ops and call again. The reference below (written for a code\n"
        "author) is YOUR reference — 'the user message' there corresponds to the data\n"
        "and design intent you have in context.\n\n"
        + VISUAL_REVIEW_POLICY + "\n\n" + ARTIFACT_VERIFICATION_POLICY + "\n\n"
        + page_reference
        + _SLIDES_CONTRACT
        + _STORAGE_CONTRACT
    )


# The planner receives this alongside the static screenshot contract above.
VISUAL_REVIEW_POLICY = """
After a successful page create/edit, review the attached screenshot if present
and permitted. Check the task's working surface, hierarchy, density, alignment,
legibility and visible states. If a material issue is visible, make at most one
focused aesthetic edit with purpose="visual_refinement" per user request. Do not redesign an existing
dashboard on an ordinary edit. Without a screenshot, skip aesthetic review; use verification_hint for interaction checks.
""".strip()
