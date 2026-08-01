# Browser Tools — agent-driven web browsing as a connection

Status: proposed (not implemented)
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
    allow_private_network: bool = Field(
        False,
        title="Target is on an internal network",
        description=(
            "Allow this connection's hosts to resolve to RFC1918 addresses. Requires "
            "BOW_BROWSER_ALLOW_PRIVATE_NETWORK on the deployment, and host patterns "
            "must name a host (no host wildcards). Loopback and link-local stay "
            "refused. See Internal / LAN targets."
        ),
    )
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
- re-checked on **each redirect hop**, against the resolved host, reusing
  `_is_safe_host` (`web_fetch.py:51`) so DNS rebinding can't walk out of the list
- after **URL normalization**: punycode/homographs, case, default ports, and userinfo —
  `https://portal.vendor.com@evil.com/` is the one that gets people

### Internal / LAN targets

The demand is real, especially self-hosted: BOW often runs *inside* the corporate
network, and the internal wiki or internal BI portal is exactly what people want the
agent to read. But "reach private addresses" is also the entire SSRF payload, so it is
layered rather than a single toggle.

**Never reachable, no toggle, no exception:**

- loopback — `127.0.0.0/8`, `::1`
- link-local — `169.254.0.0/16`, which is cloud instance metadata
  (`169.254.169.254` on AWS/GCP/Azure)
- unspecified — `0.0.0.0`, `::`

These are never a legitimate browser target and are what an SSRF is actually trying to
reach. `web_fetch._is_safe_host` (`web_fetch.py:51`) already encodes the check; the
browser reuses it.

**RFC1918 (`10/8`, `172.16/12`, `192.168/16`) needs two keys**, mirroring the pattern
`_web_search_enabled` already uses (`agent_v2.py:6115` — org switch *and* per-provider
opt-in, both required):

1. deployment env var `BOW_BROWSER_ALLOW_PRIVATE_NETWORK`, off by default — a hosted
   deployment leaves it off, so no tenant admin can turn private access on for
   themselves
2. per-connection `allow_private_network` — this specific target is internal, and that
   is intended

**The toggle narrows, it never widens.** It only removes the public-IP requirement for
hosts already matched by that connection's globs. It does not grant the LAN; it grants
`wiki.internal.corp`.

**Host patterns must be specific when it is on.** Reject wildcards in the *host* portion
of a pattern for a private-network connection: `https://wiki.internal.corp/**` is fine,
`http://10.*.*.*/**` is refused. Without this rule a private-network connection could
point the agent at BOW's own backend, the database admin UI, or anything else sharing
the container's network — with the container's network identity.

**DNS is still the weak point.** An allowlisted hostname can resolve to something else
entirely — rebinding, or just misconfigured internal DNS. Mitigation is resolve-then-pin:
resolve once at session start, validate the address against the rules above, then pin it
for the session's lifetime so the browser cannot silently re-resolve. Chromium's
`--host-resolver-rules=MAP wiki.internal.corp 10.0.1.5` launch flag looks like the right
mechanism (it binds resolution at the browser level rather than fighting Chromium's own
DNS), though it needs validating against the pinned Playwright version, and it trades
away multi-A-record load balancing — acceptable for a scoped internal target. The
`route()` interceptor re-checks every request's host regardless, so pinning is defence in
depth rather than the only control.

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
2. **`--host-resolver-rules` viability** — needs validating against the pinned Playwright
   version before the LAN story can be considered settled. If it doesn't hold, the
   fallback is routing the browser through a policy-enforcing proxy, which is more
   moving parts.
3. **Session reuse across turns within a report** — a session keyed by `report_id` alone
   would let a follow-up question continue where the last one stopped, at the cost of
   holding a context between turns. Leaning per-execution for phase 1.
4. **Category placement** — `services` is the closest existing bucket, but a browser is
   not a SaaS app. Worth a look at how the tile reads in the modal before settling.
