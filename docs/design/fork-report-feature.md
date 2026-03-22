# Fork Report Feature - Design Document

**Status:** Draft / Design Phase
**Date:** 2026-03-20
**Approach:** Option C — Summary Fork

---

## Problem Statement

When a user views a shared report (`/r/ID`) or conversation (`/c/TOKEN`), they currently have no way to build on that work. If they're logged in and have access to the same data sources (system-only auth, no per-user credentials required), they should be able to **fork** the report into their own workspace and continue the conversation.

---

## Core Concept: Summary Fork

A logged-in user can "fork" a published report or shared conversation. Instead of copying the full message history, the fork creates:

1. **An AI-generated summary** of the original conversation — what was asked, what was found, key insights
2. **References to existing data assets** — queries, steps (with their results/visualizations), and the latest artifact
3. **A fresh conversation thread** where the user can build on the summarized context

This is lightweight, avoids redundant data, and gives the AI agent clear context about prior work without replaying every message.

---

## Access Eligibility

The **Fork** button appears when ALL of these are true:

| Condition | Check |
|-----------|-------|
| User is logged in | `current_user` is not null |
| Same organization | `user.organization_id == report.organization_id` |
| All data sources use system auth | Every connection has `auth_policy = "system_only"` |
| User has data source access | Each data source is `is_public=True` OR user has `DataSourceMembership` |
| User is not the owner | `user.id != report.user_id` |

If any connection requires `auth_policy = "user_required"`, the button is either hidden or shown disabled with a tooltip explaining that user credentials are needed.

---

## What Gets Forked

| Asset | Behavior |
|-------|----------|
| **Report** | New report created, owned by forking user |
| **Data source associations** | Same data sources linked to the new report |
| **Summary completion** | Single AI-generated system message summarizing the original report |
| **Queries** | New Query records created, referencing original steps |
| **Steps** | **Referenced, not copied** — original steps linked to new widgets (step data is immutable) |
| **Widgets** | New widgets created, pointing to referenced steps |
| **Artifacts** | Latest artifact version duplicated (existing `duplicate` logic) |

**NOT forked:** Original completions, completion blocks, agent executions, tool executions, plan decisions.

---

## How the Summary Fork Works

```
Original Report
  ├── Message 1: "Show me revenue by region"
  ├── AI: [SQL query] → chart (Step A)
  ├── Message 2: "Break it down by quarter"
  ├── AI: [SQL query] → table (Step B)
  ├── Message 3: "Create a slide deck"
  └── AI: [artifact generated] (Artifact v3)

Forked Report
  ├── [Forked from "Revenue Analysis" by @alice]  ← banner with link
  │
  ├── 🔒 Summary (system message, read-only)
  │   │
  │   │  "This report analyzed revenue data across regions and quarters.
  │   │   Key findings:
  │   │   - Revenue by region chart shows APAC leading at $4.2M (Step A)
  │   │   - Quarterly breakdown reveals Q3 spike across all regions (Step B)
  │   │   - A slide deck was generated summarizing the analysis (Artifact)"
  │   │
  │   ├── Widget: "Revenue by Region" → Step A (referenced, chart)
  │   ├── Widget: "Quarterly Breakdown" → Step B (referenced, table)
  │   └── Artifact: slide deck (duplicated, v1 in new report)
  │
  └── [Your conversation starts here]  ← user types new messages
      ├── Message 1: "Now add profit margins to the regional view"
      └── AI: [new SQL query] → new Step C
```

### Why Summary over Full Copy

| | Summary Fork | Full Snapshot Fork |
|--|---|---|
| **Data volume** | Minimal — one summary message | All messages + blocks copied |
| **AI context quality** | Distilled, high-signal context | Raw conversation (noise + signal) |
| **Fork speed** | Fast (1 LLM call + lightweight DB ops) | Slower (N completions + blocks copied) |
| **Long conversations** | Scales well — summary stays concise | Grows linearly with conversation length |
| **Data asset access** | References original steps (no duplication) | Same |
| **Tradeoff** | Loses nuance of intermediate reasoning | Preserves full history |

---

## Summary Generation

The summary is generated server-side during the fork operation using a focused LLM call.

### Input to summary generator

The summary prompt uses the same XML format as `queries_section.py` to reference
queries and visualizations by ID, so the agent can consistently resolve them later.

```xml
Report title: "{title}"
Data sources: {list of data source names}

Conversation ({N} messages):
{formatted messages from _get_report_messages()}

Created assets:

<query id="{query_id}" title="{query_title}">
  <description>{query_description}</description>
  <step id="{step_id}" title="{step_title}" type="{step_type}" status="{step_status}">
    <code>{step_code (SQL/Python)}</code>
    <description>{step_description}</description>
  </step>
  <visualization id="{viz_id}" title="{viz_title}">
    <view>{viz_view JSON summary}</view>
  </visualization>
</query>

<query id="{query_id_2}" title="...">
  ...
</query>

<artifact id="{artifact_id}" title="{artifact_title}" mode="{page|slides}">
  {artifact content outline}
</artifact>
```

This matches the format from `queries_section.py` (`xml_tag("query", ..., {"id": ..., "title": ...})`)
and the `viz_id: {id}` references in `message_context_builder.py` tool execution digests. The agent
will recognize these IDs when the user asks follow-up questions about specific queries or charts.

### Prompt template

```
Summarize this data analysis conversation for someone who wants to continue
the work. Include:
1. What questions were asked and what data was explored
2. Key findings and insights discovered
3. What data assets were created — reference each by its query/visualization ID
4. Any open threads or areas not yet explored

Keep it concise (3-8 sentences). Use specific numbers and findings from the
step results where available. Reference created assets using the format
`viz_id: <id>` for visualizations and `query: <title> (id=<id>)` for queries,
so the system can resolve them.
```

### Output

A structured summary stored as the first completion in the forked report, with:
- `role = "system"`
- `is_fork_summary = True`
- `source_report_id = original.id`
- `fork_asset_refs` (JSON): list of `{type, id, title}` for all referenced queries, visualizations, and artifacts — enables the frontend to render inline previews
- Rich content that references the widgets/steps by ID and name

---

## Schema Changes

### Report model

```python
# New fields on Report
forked_from_id = Column(String(36), ForeignKey('reports.id'), nullable=True)
```

### Completion model

```python
# New fields on Completion
is_fork_summary = Column(Boolean, default=False)  # marks the fork summary message
source_report_id = Column(String(36), nullable=True)  # origin report for attribution
fork_asset_refs = Column(JSON, nullable=True)  # [{type: "query"|"visualization"|"artifact", id: str, title: str}]
```

### ReportSchema changes

```python
# Expose fork lineage in ReportSchema
forked_from_id: Optional[str]  # parent report ID
forked_from_title: Optional[str]  # resolved via relationship for display
forked_from_user_name: Optional[str]  # original author name for attribution
```

No changes needed on Step, Widget, Query, or Artifact models — we use existing fields and relationships.

---

## API Design

### Fork endpoint

```
POST /api/reports/{report_id}/fork
```

**Request:**
```json
{
  "title": "My analysis (optional, defaults to 'Fork of {original_title}')"
}
```

**Response:**
```json
{
  "id": "new-report-uuid",
  "title": "Fork of Revenue Analysis",
  "forked_from_id": "original-report-uuid",
  "slug": "fork-of-revenue-analysis-abc123"
}
```

**Permission:** `create_reports` + data source access checks.

**Note:** This is an async-feeling operation. The endpoint creates the report and redirects immediately. The summary completion is generated in the background (or streamed to the client via WebSocket once ready).

### Fork eligibility (returned with existing endpoints)

Add to `/r/{report_id}` and `/c/{token}` response:

```json
{
  "fork_eligibility": {
    "can_fork": true,
    "reason": null
  }
}
```

Possible reasons when `can_fork = false`:
- `"not_logged_in"` — user is anonymous
- `"different_org"` — user is in a different organization
- `"user_auth_required"` — one or more connections require per-user credentials
- `"no_data_source_access"` — user lacks access to one or more data sources
- `"is_owner"` — user already owns this report

---

## Frontend Changes

### /r/[id] and /c/[token] pages

- Show **Fork** button in the header/toolbar when `fork_eligibility.can_fork == true`
- Button style: secondary/outline, with a fork icon
- On click: call `POST /api/reports/{report_id}/fork`, then redirect to `/reports/{new_id}`
- If `can_fork == false`, show disabled button with tooltip showing the reason

### /reports/[id] (chat editor) — forked report

#### Fork lineage banner

- Persistent banner at the top of the report: **"Forked from [Original Report Title]"**
  - Links to the original report (`/r/{forked_from_id}`)
  - Shows original author name
  - If this is a fork-of-a-fork, show full lineage chain: "Forked from X → originally from Y"
- The `forked_from_id` is stored on the Report model and exposed in `ReportSchema`
- Banner is always visible (not dismissible) — serves as attribution

#### Summary message card

- Summary completion renders as a special system card:
  - Distinct visual style (muted background, fork icon, "Summary of original analysis" label)
  - Not editable or deletable
  - Contains the AI-generated summary text

#### Forked Queries Panel (new component: `ForkedQueriesPanel`)

Below the summary card, render **all inherited queries together** in a grouped panel.
This is similar to `ToolWidgetPreview` but displays all forked queries as a unified block
rather than individual inline previews scattered across messages.

```
┌─────────────────────────────────────────────────────┐
│  📊 Inherited Queries (3)                     [▼]   │
│                                                     │
│  ┌─ Revenue by Region ──────────────────────────┐   │
│  │  [Chart]  [Data]  [Code]       viz_id: abc   │   │
│  │  ┌──────────────────────┐                    │   │
│  │  │   bar chart render   │      query: def    │   │
│  │  └──────────────────────┘                    │   │
│  └──────────────────────────────────────────────┘   │
│                                                     │
│  ┌─ Quarterly Breakdown ────────────────────────┐   │
│  │  [Chart]  [Data]  [Code]       viz_id: ghi   │   │
│  │  ┌──────────────────────┐                    │   │
│  │  │   table render       │      query: jkl    │   │
│  │  └──────────────────────┘                    │   │
│  └──────────────────────────────────────────────┘   │
│                                                     │
│  ┌─ Top Customers ──────────────────────────────┐   │
│  │  [Chart]  [Data]  [Code]       viz_id: mno   │   │
│  │  ...                                         │   │
│  └──────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

**Component design:**

- Reuses `ToolWidgetPreview` rendering internals (chart/data/code tabs, `RenderVisual`, `RenderTable`)
- But wraps them in a **grouped container** with a shared header ("Inherited Queries")
- Each query card shows:
  - Query title + description
  - Tabbed view: Chart | Data | Code (same as `ToolWidgetPreview`)
  - Query ID and Visualization ID displayed as subtle badges (for agent context continuity)
- Props: `{ queries: Array<{ query_id, step, visualization, widget_id }>, readonly: true }`
- Always `readonly=true` — edit/save disabled; user creates new queries via conversation
- Collapsible as a group and individually per-query
- If artifact exists, show artifact preview at the end of the panel

#### Conversation area

- Clear visual separator ("Your conversation starts here") below the forked queries panel
- New messages from the user render normally below the separator
- New tool executions render inline as standard `ToolWidgetPreview` (not in the forked panel)

---

## Backend Flow (Fork Service)

```
1. Validate eligibility
   ├── User is authenticated
   ├── Report exists and is published/shared
   ├── User is in same org
   ├── All connections are system_only
   └── User has access to all data sources

2. Create new Report
   ├── title = request.title or "Fork of {original.title}"
   ├── user_id = current_user.id
   ├── organization_id = current_user.organization_id
   ├── forked_from_id = original.id
   ├── status = "draft"
   └── mode = original.mode

3. Link data sources
   └── Copy report_data_source_association entries

4. Create widgets + link steps
   ├── For each Widget in original report that has successful steps:
   │   ├── Create new Widget in new report
   │   ├── Create new Query in new report (title, description from original)
   │   └── Link original Step records to new Widget
   │       (steps are immutable — safe to reference across reports)
   └── Copy dashboard layout (DashboardLayoutVersion) if exists

5. Duplicate latest artifact (if exists)
   └── Use existing ArtifactService.duplicate() logic
   └── Reset version to 1 in new report

6. Generate summary (async / background)
   ├── Gather context:
   │   ├── Original report title + description
   │   ├── Completions (via _get_report_messages)
   │   ├── Query titles + descriptions
   │   ├── Step titles, types, code snippets, descriptions
   │   └── Artifact title + mode + content outline
   ├── Call LLM with summary prompt
   ├── Create Completion:
   │   ├── role = "system"
   │   ├── is_fork_summary = True
   │   ├── source_report_id = original.id
   │   ├── turn_index = 0
   │   └── completion = { summary text }
   └── Broadcast via WebSocket to report subscribers

7. Return new report (immediately after step 5, don't wait for summary)
```

---

## AI Agent Context Integration

When the user sends their first message in the forked report, the agent needs context about the prior work. The summary completion serves this purpose naturally:

1. **ContextHub** already builds context from report completions via `_get_report_messages()`
2. The fork summary (turn_index=0) will be the first message the agent sees
3. Steps referenced by the forked widgets provide schema/data context
4. The agent can reference existing steps by name ("the Revenue by Region chart shows...")

No special context builder needed — the existing message + widget + step context pipeline handles it.

---

## Open Questions

1. **Fork attribution visibility** — Should the original author see "5 people forked this"?
2. **Cross-org forking** — Strictly same-org only, or allow cross-org if data sources permit?
3. **Summary quality** — Should we expose a "regenerate summary" action if the user finds it lacking?
4. **Fork of a fork** — Allow it? `forked_from_id` points to immediate parent, not root. Summary would summarize the forked report (which includes its own summary).
5. **Notifications** — Notify the original author when their report is forked?
6. **Step freshness** — Referenced steps show data from when they were originally run. Add a "re-run" button on forked widgets?

---

## Future Extensions

- **Fork with selection** — Let user pick which widgets/steps to include before forking
- **Merge back** — If the forked user discovers something useful, suggest it back to the original
- **Fork gallery** — Show all public forks of a popular report
- **Template reports** — Reports explicitly designed to be forked (starter templates)
- **Custom summary prompt** — Let user add a note ("I want to focus on APAC region") that gets included in the summary generation
