# Browser Tools — agent-driven web browsing as a connection

Status: phase 1 implemented (see `Implementation notes` at the end)
Scope: a `browser` connection type, five browser tools backed by Playwright, per-agent
tool policy via the existing overlay, an org-level capability flag, and inline rendering
of screenshots in chat.
Out of scope (deliberately, see [Deferred](#deferred-phase-2)): human takeover, a live
browser panel, credential/profile persistence, and therefore authenticated browsing in
scheduled tasks.

## Summary

Give the agent a real browser so it can reach things HTTP alone can't: JS-rendered
pages, multi-step flows, and portals that only expose data through a UI. The payoff for
BOW specifically is the handoff — **browser → downloaded file → `inspect_data` /
`read_excel_as_csv` / `create_data` → widget** — which turns "the numbers only exist in
a vendor portal" into a normal report.

Four decisions shape everything below:

1. **The connection is the gate.** There is no org-level capability flag: browser tools
   exist for a report only when an admin has created a `browser` connection and attached
   it to an agent in scope. Reach is always the connection's URL allowlist — there is no
   unscoped browsing lane.
2. **Snapshot + refs, not vision.** The primary channel is an accessibility-tree
   snapshot where each interactive element carries a ref (`[ref=e12]`); the model picks
   a ref and the tool resolves it to a Playwright locator. Screenshots are a separate,
   opt-in tool. A typical page is ~1.5 KB as a snapshot versus tens of KB as an image.
3. **Tool granularity follows the policy boundary, not the API surface.** Because each
   tool becomes a `ConnectionTool` row with `allow | confirm | deny`, the split exists so
   an admin can say "reading is fine, interacting needs confirmation."
4. **Headless and ephemeral.** No persisted profile, so no credential store is needed
   yet. A login wall is *detected and reported*, not worked around.

Nothing here needs a new table, a new policy system, or a new scoping mechanism.

## Why the connection is the only gate

Browser tools are **connection-provided**, exactly like MCP and Custom API tools: no
connection, no tools registered. That makes a separate `enable_browser_use` org boolean
redundant — creating a connection already requires the org-level `manage_connections`
permission (`permissions_registry.py:29`), which is the same permission that lets someone
attach an arbitrary MCP server and execute arbitrary remote tools. A browser target
scoped to a URL list is a *narrower* grant than that, so gating it identically is
consistent.

`web_fetch` needs an org flag because it is a builtin that exists whether or not anyone
configured anything. Browser tools don't have that problem.

This also means there is **no unscoped browsing lane and no "allow all" checkbox**. Every
browser call is bounded by some connection's URL patterns. The ad-hoc "just read this
public page" case is already served by `web_fetch`; the browser exists for the harder
cases, which are almost always a specific known site — the thing you would configure
anyway.

What stays org-level is only what a connection genuinely cannot express: the resource
ceiling. A connection admin creating five browser targets should not be able to exhaust
the backend's memory, and concurrency is a process-wide property, not a per-target one.

## Data model

**No migration.** Every piece already exists:

| Concern | Existing model |
| --- | --- |
| The target + its URL list | `Connection` (`type='browser'`, `credentials` NULL, `auth_policy='system_only'`) |
| One row per browser tool, org-wide `is_enabled` + `policy` | `ConnectionTool` |
| Per-agent enable/disable + policy override | `DataSourceConnectionTool` |
| Per-user preference | `UserConnectionTool` |
| Attaching a target to an agent | `domain_connection` |
| Report scope | `report_data_source_association` |

Effective policy resolves through the same
`ToolPolicyService` / `resolve_effective_policy` path MCP tools use, so a browser tool
inherits confirmations, audit, and the per-agent tool UI with no new code.

One wrinkle: `ConnectionTool` rows are normally *discovered* from an MCP server. A
browser connection has nothing to introspect, so the create path **seeds a fixed set of
five rows**. "Test connection" becomes "can we reach these URL patterns", which is cheap
and genuinely useful.

## Registry entry

`app/schemas/data_source_registry.py`, modelled on the `mcp` entry:

```python
"browser": DataSourceRegistryEntry(
    type="browser",
    category="services",
    title="Browser",
    description=(
        "Let agents browse a specific set of web pages — read content, follow "
        "links, and download files. Scoped to the URLs you list."
    ),
    config_schema=BrowserConfig,
    credentials_auth=AuthOptions(
        default="none",
        by_auth={"none": AuthVariant(title="No Auth", schema=BrowserNoAuthCredentials,
                                     scopes=["system"])},
    ),
    client_path="app.data_sources.clients.browser_client.BrowserClient",
    version="beta",
    is_connection=False,     # tool provider, not a data source
    data_shape="tools",
    catalog_ownership="none",
    ui_form="browser",       # new lean form: name + URL patterns
),
```

`BrowserConfig` (`app/schemas/data_sources/configs.py`):

```python
class BrowserConfig(BaseModel):
    url_patterns: list[str] = Field(
        ...,
        title="Allowed URLs",
        description=(
            "Glob patterns the agent may visit, e.g. https://portal.vendor.com/**. "
            "Anything not matched is refused."
        ),
        json_schema_extra={"ui:type": "stringlist"},
    )
    allow_downloads: bool = Field(True, title="Allow downloads")
```

Icon: `frontend/public/data_sources_icons/browser.png` (resolved by `DataSourceIcon` via
the normalized type token — no component change needed).

### Granularity

One connection per logical target — "Vendor Portal", "Internal Wiki" — each with its own
patterns. A single org-wide mega-connection collapses per-agent attachment to a global
on/off and throws away the control the overlay gives for free.

## The tools

Five, chosen so each is a distinct policy posture:

| Tool | Input | Returns | Default policy |
| --- | --- | --- | --- |
| `browser_navigate` | `url`, `session_id?` | url, title, compact snapshot | allow |
| `browser_snapshot` | `session_id`, `full?`, `max_chars?` | a11y tree with refs | allow |
| `browser_extract` | `session_id`, `query?` | bounded readable text | allow |
| `browser_act` | `session_id`, `ref`, `action`, `text?` | result + fresh snapshot | **confirm** |
| `browser_vision` | `session_id` | `file_id` + optional vision block | allow (separately deniable) |

`browser_act`'s `action` enum folds click / type / press / scroll / hover / select /
dialog into one tool, so the write posture is a single toggle rather than six.

**Deliberately excluded:**

- **Raw CDP passthrough.** Navigates anywhere, reads any cookie, ignores the allowlist.
  It is arbitrary code execution wearing a browser costume.
- **JS console / `evaluate`.** Same hole. Arbitrary JS in page context means `fetch()`
  from inside the origin and `document.cookie` — the allowlist stops being a security
  control. In a single-user local agent the operator is the trust boundary; here the
  allowlist is, and nothing may evaluate attacker-reachable code inside it.
- **Web search.** Already native (`_web_search_enabled`), not duplicated here.

### Snapshot format and refs

```
- heading "Monthly reports" [ref=e3]
- combobox "Period" [ref=e7] value="2026-07"
- button "Export CSV" [ref=e12]
```

Role, accessible name, optional state. Non-interactive containers stripped in compact
mode; truncated at `max_chars` with an explicit marker.

Two properties worth building in from the start:

- **Typed stale-ref error.** Refs are scoped to the snapshot that produced them; after
  navigation or a DOM change they are invalid. A distinct `stale_ref` error (rather than
  a generic timeout) lets the observation say *re-snapshot, don't retry* — a far better
  signal than a failed click.
- **`[new]` markers.** On a repeat snapshot of the same page, flag elements that appeared
  since the previous one, so a re-snapshot is a cheap diff instead of a full re-read.

### Session management

`BrowserSessionManager`, keyed by `(report_id, execution_id)`:

- lazily launches a headless Chromium context on the first `browser_navigate`
- TTL eviction plus a hard cap on concurrent contexts (~150 MB each, in-process)
- torn down at the end of the run — no state survives the turn
- `session_id` returned to the model so multi-step flows stay on one context

Playwright and Chromium are already installed and driven in-process
(`thumbnail_service.py`, `report_pdf_service.py`, `Dockerfile:46`), so this adds no new
runtime dependency.

> On a shared multi-tenant deployment the memory ceiling is the binding constraint. If
> concurrency becomes a problem, the manager is the seam to swap for an out-of-process
> browser service without touching the tools.

## Security

### Allowlist enforcement

Matching is **glob only**. Regex is excluded on purpose: unanchored patterns and `.`
silently matching any character (`vendor.com` matching `vendorxcom`) are the standard
allowlist-bypass shape, and admin-supplied patterns add ReDoS surface.

Enforcement happens in a Playwright `route()` interceptor, not at the tool boundary:

- checked on **every request** — subresources, XHR, redirects — not just top-level
  navigation
- re-checked on **each redirect hop**, against the pattern list, so a redirect cannot
  walk out of the allowlist
- after **URL normalization**: punycode/homographs, case, default ports, and userinfo —
  `https://portal.vendor.com@evil.com/` is the one that gets people

### Internal / LAN targets

**Internal targets are a first-class capability, governed by the connection's URL
patterns and nothing else.** No org setting, no deployment env var, no per-connection
"allow private network" toggle. If an admin lists `https://wiki.internal.corp/**`, the
agent may browse it.

This is deliberate, and it is not a relaxation — it is applying the right threat model.
The private-address block (`_is_safe_host`) exists in exactly two places, `web_fetch`
and `SafeHttpClient`, and the latter's docstring names the reason: both are
*model-facing* surfaces where **the model supplies the host**. That is SSRF via prompt
injection, and the IP-range check is the correct defence.

A browser connection is not that. Its URL list is authored by an admin holding
`manage_connections` — the same permission that already points a Postgres or Custom API
connection at `10.0.1.5`, which no data source client blocks and none should. The model
cannot introduce a host: the `route()` interceptor confines every request to the admin's
patterns. Inside that allowlist the IP check defends against a threat the allowlist has
already eliminated, so applying it there would import a model-facing control into
admin-facing configuration.

For self-hosted deployments — BOW running inside the corporate network, where the
internal wiki and internal BI portal are the *primary* things worth browsing — this is
the difference between the feature working and the feature being pointless.

Two guardrails remain, neither a security boundary:

- **Link-local (`169.254.0.0/16`) is always refused**, including `169.254.169.254` —
  cloud instance metadata. It is never a legitimate browsing target, so allowing it
  could only ever be a mistake.
- **The host portion of a pattern must name a host** — a hostname (wildcarded subdomains
  are fine: `https://*.internal.corp/**`) or a single literal IP. Patterns whose host
  spans a network, like `http://10.*.*.*/**`, are refused at validation with a clear
  message. Such a pattern isn't a browsing target, it's a network scan, and it is how a
  connection would end up aimed at BOW's own backend with the container's network
  identity.

DNS rebinding is worth noting and not worth building for here: an attacker would need to
control resolution for a hostname an admin explicitly allowlisted, at which point they
already control that internal host. Pinning resolution per session (e.g. Chromium's
`--host-resolver-rules`) stays available as later hardening if a deployment wants it.

### Redaction

Snapshots serialize input values, and screenshots capture whatever is on screen. Without
a redaction pass, a typed password lands in `tool_executions.result_json`, then the
observation, then model context, then the audit log.

So, from the first commit — not when credentials arrive:

- values of `input[type=password]` and secret-shaped fields (name/autocomplete hints)
  are stripped from every snapshot
- those fields are masked in the DOM *before* `screenshot()`, not after
- a tool-input validator refuses a raw secret as an argument, so "just paste your
  password in chat" cannot work

This has to ship in v1 **because** credentials come later: by the time the secret store
lands there would otherwise be months of unredacted snapshot text already persisted.

### Prompt injection

Page content becomes model input while the model holds an actuator. Mitigations are the
allowlist (the agent can only read pages an admin chose to trust) and `browser_act`
defaulting to `confirm`. Neither is complete; the combination is what makes the default
posture safe.

## Org settings

Only resource limits — there is no capability flag, per
[Why the connection is the only gate](#why-the-connection-is-the-only-gate).
`app/schemas/organization_settings_schema.py`:

| Setting | Default | Purpose |
| --- | --- | --- |
| `browser_max_concurrent_sessions` | `3` | Memory ceiling per org |
| `browser_session_ttl_minutes` | `10` | Idle eviction |

## Frontend

**No `Browser.vue` and no right-panel view.** Without takeover there is nothing to
interact with, and a screenshot on the last step plus one on every failure covers what
watching would have shown.

### Tool cards

- `BrowserVisionTool.vue` — mirrors `GenerateImageTool.vue`: `AuthenticatedImage` bound
  to `result_json.screenshot_file_id`, ~40 lines.
- `BrowserTool.vue` — compact line for the other four: favicon, title, URL, and for
  `browser_act` the action plus the element's accessible name ("clicked *Export CSV*").

### Grouping

`GROUPABLE_TOOLS` (`useBlockGrouping.ts:24`) collapses runs of low-signal tools into one
ticker line. The precedent is exact — `web_fetch` is in the set, `generate_image` is not:

- **add**: `browser_navigate`, `browser_snapshot`, `browser_extract`, `browser_act`
- **omit**: `browser_vision`, so a screenshot renders as a real card

A 15-step browsing run becomes one collapsed line — "9 steps · Browsing
portal.vendor.com · 22s" — plus a screenshot, rather than fifteen stacked rows.

## Screenshots: rendering and vision are separate decisions

`agent_v2.py:3803` already extracts `observation["images"]` into vision blocks, strips
the base64 from the serialized observation, and carries images on the **last** observation
only. So the two paths are independently controllable:

| Path | Cost | Policy |
| --- | --- | --- |
| `result_json.screenshot_file_id` → UI | a file row + bytes | liberal — auto-capture on the final step of a run and on every blocked/error step |
| `observation["images"]` → vision tokens | expensive | only when the model called `browser_vision` |

The user sees what happened without the agent having to decide to look, and nobody pays
context for it.

Because the screenshot is a real `File`, the `generate_image → file_id →
create_artifact` chain (`generate_image.py:56`) works unchanged: a screenshot can be
embedded in a dashboard with `<BowFile id="...">`. The tool description must say so
explicitly or the model won't discover it.

## Context handling

A new `browser` branch in `observation_context_builder.py:130`, next to `web_fetch`: on
**stale** observations drop `snapshot` and `images`, keep `url` / `title` / `summary`.
Browser observations are the largest the agent produces; without compaction a 20-step run
buries the schemas and instructions it needs.

## Blocked on login

When a page presents a login form, MFA challenge, captcha, or consent interstitial, the
tool returns a typed blocked-observation rather than clicking hopefully:

```json
{
  "summary": "Blocked: authentication required at https://portal.vendor.com/login",
  "blocked_reason": "authentication",
  "url": "https://portal.vendor.com/login"
}
```

The agent surfaces that plainly to the user. "I can't get past this login" after one
step beats eight turns of guessing, and it is the honest state of the feature until
phase 2.

## Phasing

**Phase 1 (this design)** — connection type + registry entry + icon; five tools;
session manager; allowlist interceptor + redaction; org settings; tool cards and
grouping; download → `File` handoff.

**Phase 1.5** — the download handoff is the product win, so it deserves explicit
polish: downloads land in the file store with the originating URL recorded, and the
tool description points the model at `inspect_data` / `read_excel_as_csv` next.

## Deferred (phase 2)

**Human takeover and credential persistence are one feature, and neither is worth much
alone.** Takeover's value is that it populates a durable profile — sign in once, reuse
for weeks. Without persistence the session dies with the run, so the user would take over
on *every* run: a toll booth, not a feature. And persistence without takeover means
someone typing portal credentials into a form, which is the flow takeover exists to
avoid.

So they ship together, with:

- an encrypted per-`(user, connection)` **profile directory** rather than a
  `storage_state` blob — a profile carries IndexedDB, service workers, and device-trust
  tokens, which is exactly what MFA "remember this device" relies on. A blob loses those
  and every scheduled run re-triggers MFA.
- the connection flipping to `auth_policy='user_required'`; the URL list is unchanged.
  This is additive — the connection keeps its shape and gains an auth mode.
- a headed browser behind noVNC for the live view, since VNC is bidirectional and hands
  back the input path for free.
- **"save this login"** at the end of a takeover as how profiles get created — nobody
  fills in a credentials form, and the credential arrives pre-validated.

**Known consequence of deferring:** browser tools cannot reach authenticated portals from
scheduled tasks or automations in phase 1. That is a deliberate limitation, not a bug.

A deployment note for phase 2: profile directories need persistent volumes, which is a
change for the `k8s/` setup and worth confirming before committing to that shape.

## Open questions

1. **`browser_extract` vs `browser_snapshot`** — is a separate bounded-text extraction
   tool earning its slot, or should `snapshot(full=true)` cover it? Leaning: keep it,
   because extraction wants a different truncation budget than interaction.
2. **Pattern validation strictness** — "the host must name a host" needs a precise rule
   (and a good error message) before it is implemented, since it is the one structural
   guard on internal targets.
3. **Session reuse across turns within a report** — a session keyed by `report_id` alone
   would let a follow-up question continue where the last one stopped, at the cost of
   holding a context between turns. Leaning per-execution for phase 1.
4. **Category placement** — `services` is the closest existing bucket, but a browser is
   not a SaaS app. Worth a look at how the tile reads in the modal before settling.

## Implementation notes (phase 1)

What shipped, and where it differs from the proposal above:

- **Gating is capability-based, exactly like file tools.** A new `Capability.BROWSER`
  (`data_sources/clients/base.py`) is declared by `BrowserClient`
  (`data_sources/clients/browser_client.py`); each tool sets
  `requires_capability="browser"`, so the five tools enter the catalog only when a
  browser connection is attached to the report (`agent_v2` already unions attached
  connections' capabilities). No new gating machinery, and no `ConnectionTool` seeding
  was needed for phase 1.
- **Snapshots use Playwright's native `aria_snapshot(mode="ai")`** — it emits the
  `[ref=eN]` tree and `page.locator("aria-ref=eN")` resolves a ref to a locator, so we
  did not hand-roll a ref system. A ref that no longer resolves surfaces as the typed
  `stale_ref` error.
- **Redaction is enforced** (`_browser_common.build_snapshot` blanks secret-shaped input
  values for the duration of the snapshot; `mask_secrets_style` masks them before a
  screenshot). Verified e2e: a password typed into the page appears in neither the
  snapshot, the extracted text, nor the screenshot.
- **Allowlist** is enforced by a `context.route("**/*")` interceptor: the main document
  must match the connection's patterns; other requests must either match or resolve to a
  public host (so public CDN subresources load but SSRF to a non-listed internal host is
  refused); link-local literals are always refused.
- **Proxy awareness:** the session honors `HTTPS_PROXY`/`NO_PROXY` so it works behind an
  egress proxy. `BOW_BROWSER_IGNORE_HTTPS_ERRORS` is a **sandbox/dev-only** escape hatch
  for a MITM dev proxy and must stay unset in production.
- **Frontend:** `BrowserTool.vue` renders navigate/snapshot/extract/act as a collapsed
  line that expands on click; `BrowserVisionTool.vue` shows the screenshot inline. The
  four low-signal tools are in `GROUPABLE_TOOLS`; `browser_vision` is not, so a run
  collapses to one ticker line plus the screenshot card.

Deferred within phase 1 (not blockers, called out honestly):

- **`browser_act` has no interactive confirmation yet.** The design wants it to default to
  "confirm"; phase 1 relies on the allowlist as the boundary and executes acts directly.
  The per-agent `allow | confirm | deny` policy is the natural home for this and is the
  first follow-up.
- **Downloads** are wired (saved to the file store via the download handler) but the
  browse→download→`inspect_data` chain hasn't been exercised e2e.
