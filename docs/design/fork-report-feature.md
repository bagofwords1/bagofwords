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

### Data chain context

The artifact rendering pipeline relies on a specific chain:

```
Artifact.content.visualization_ids → Visualization records → Query records
                                                          → Step records (via Query.default_step)
```

`ArtifactFrame` fetches `/api/queries?report_id={reportId}&artifact_id={artifactId}` which:
1. Loads the Artifact, reads `content.visualization_ids`
2. Finds Visualization records matching those IDs
3. Returns the Query records that own those visualizations

**This means:** for the artifact to render in the forked report, the forked report must own
the Query and Visualization records (they're filtered by `report_id`). Steps can be shared
across reports since they're accessed via `Query.default_step_id`, but Queries and Visualizations
must be duplicated.

### Fork execution flow

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

4. Duplicate queries, visualizations, and widgets
   │
   │  Build an ID remapping table as we go:
   │  old_query_id → new_query_id
   │  old_viz_id → new_viz_id
   │  old_widget_id → new_widget_id
   │
   ├── For each Query in original report:
   │   ├── Create new Query in forked report
   │   │   ├── report_id = new_report.id
   │   │   ├── user_id = current_user.id
   │   │   ├── organization_id = same
   │   │   ├── title, description = copied from original
   │   │   ├── default_step_id = original.default_step_id  ← SHARED, not copied
   │   │   └── Record mapping: old_query_id → new_query_id
   │   │
   │   └── For each Visualization on original Query:
   │       ├── Create new Visualization in forked report
   │       │   ├── report_id = new_report.id
   │       │   ├── query_id = new_query_id (remapped)
   │       │   ├── title, view, status = copied from original
   │       │   └── Record mapping: old_viz_id → new_viz_id
   │       └── (Steps are NOT duplicated — Query.default_step_id
   │            points to original Step, which is immutable)
   │
   ├── For each Widget in original report:
   │   ├── Create new Widget in forked report
   │   │   ├── report_id = new_report.id
   │   │   ├── title, x, y, width, height = copied
   │   │   └── Record mapping: old_widget_id → new_widget_id
   │   └── Re-link Steps to new Widget
   │       └── For each Step pointing to old widget:
   │           └── Associate with new_widget_id
   │           NOTE: Steps have widget_id FK — but steps are shared.
   │           Instead, create a NEW Step record that wraps the original
   │           (same code, data, status) but with new widget_id.
   │           OR: use the Query.default_step_id pattern and skip widget-step linking.
   │           DECISION: Use Query.default_step_id for data access. Widget is for
   │           dashboard layout only. Steps don't need new widget_id — the frontend
   │           accesses step data via GET /api/queries/{queryId}/default_step.
   │
   └── Copy dashboard layout (DashboardLayoutVersion) if exists
       └── Remap widget IDs in the layout JSON using the mapping table

5. Duplicate latest artifact with remapped visualization_ids
   ├── Get latest artifact: ArtifactService.get_latest_by_report(original.id)
   ├── Create NEW Artifact (not using duplicate() — we need to remap IDs):
   │   ├── report_id = new_report.id
   │   ├── user_id = current_user.id
   │   ├── organization_id = same
   │   ├── title, mode = copied from original
   │   ├── version = 1
   │   ├── content = {
   │   │     "code": original.content["code"],
   │   │     "visualization_ids": [viz_id_map[old_id] for old_id in original viz_ids]
   │   │   }
   │   │   ↑ CRITICAL: remap old viz IDs to new viz IDs so
   │   │     GET /api/queries?report_id=NEW&artifact_id=NEW resolves correctly
   │   └── status = "completed"
   ├── Copy thumbnail (same logic as ArtifactService.duplicate)
   └── The artifact's JSX code references visualizations by position/index
       via useArtifactData() — the code itself doesn't embed viz IDs,
       so no code rewriting needed, only content.visualization_ids remapping

6. Generate summary (async / background)
   ├── Gather context:
   │   ├── Original report title + description
   │   ├── Completions (via _get_report_messages)
   │   ├── NEW query IDs + titles + descriptions (use remapped IDs!)
   │   ├── Step titles, types, code snippets, descriptions
   │   ├── NEW visualization IDs + titles (use remapped IDs!)
   │   └── NEW artifact ID + title + mode
   ├── Call LLM with summary prompt
   ├── Create Completion:
   │   ├── report_id = new_report.id
   │   ├── role = "system"
   │   ├── is_fork_summary = True
   │   ├── source_report_id = original.id
   │   ├── turn_index = 0
   │   ├── completion = { summary text with NEW IDs }
   │   └── fork_asset_refs = [
   │         {type: "query", id: new_query_id, title: "..."},
   │         {type: "visualization", id: new_viz_id, title: "..."},
   │         {type: "artifact", id: new_artifact_id, title: "..."}
   │       ]
   └── Broadcast via WebSocket to report subscribers

7. Return new report (immediately after step 5, don't wait for summary)
```

### Why queries & visualizations must be duplicated (not just referenced)

The `/api/queries` endpoint filters by `report_id`. `ArtifactFrame` calls
`/api/queries?report_id={NEW_REPORT_ID}&artifact_id={NEW_ARTIFACT_ID}`.
If we only referenced original queries, they'd have `report_id = ORIGINAL`
and wouldn't be returned for the new report.

Similarly, Visualizations have `report_id` and `query_id` foreign keys that
must point to the new report and new queries.

**Steps are the exception** — they're accessed via `Query.default_step_id`
(a direct FK, not filtered by report_id), so sharing them across reports works.

---

## AI Agent Context Integration

When the user sends their first message in the forked report, the agent needs context about the prior work. The summary completion serves this purpose naturally:

1. **ContextHub** already builds context from report completions via `_get_report_messages()`
2. The fork summary (turn_index=0) will be the first message the agent sees
3. `queries_section.py` builds `<query id="..." title="...">` context from the forked report's queries — which now have **new IDs** matching what's in the summary
4. The summary references the same new query/viz IDs, so the agent sees consistent references
5. The agent can reference existing queries by name or ID ("the Revenue by Region chart shows...")

No special context builder needed — the existing message + widget + query + step context pipeline handles it because we duplicated queries/visualizations with proper report_id ownership.

---

## Implementation Plan

### Phase 1: Schema & Migration
**Files to modify:**
- `backend/app/models/report.py` — add `forked_from_id` column
- `backend/app/models/completion.py` — add `is_fork_summary`, `source_report_id`, `fork_asset_refs` columns
- `backend/app/schemas/report_schema.py` — add `forked_from_id`, `forked_from_title`, `forked_from_user_name`
- New alembic migration

### Phase 2: Fork Service (core backend logic)
**New file:** `backend/app/services/fork_service.py`

```python
class ForkService:
    async def check_eligibility(db, report_id, user) -> ForkEligibility
    async def fork_report(db, report_id, user, title=None) -> Report
    async def _duplicate_queries_and_visualizations(db, original_report, new_report, user) -> IdMap
    async def _duplicate_artifact_with_remapped_ids(db, original_report, new_report, user, viz_id_map) -> Artifact
    async def _duplicate_widgets_and_layout(db, original_report, new_report, widget_id_map)
    async def _generate_fork_summary(db, original_report, new_report, id_maps) -> Completion
```

### Phase 3: API Route
**File to modify:** `backend/app/routes/report.py`
- `POST /reports/{report_id}/fork` — calls ForkService
- Modify `/r/{report_id}` and `/c/{token}` responses to include `fork_eligibility`

### Phase 4: Frontend — Fork Button
**Files to modify:**
- Public report page (`/r/[id]`) — add Fork button
- Public conversation page (`/c/[token]`) — add Fork button
- Wire up `POST /api/reports/{report_id}/fork` call + redirect

### Phase 5: Frontend — Forked Report UI
**New components:**
- `components/reports/ForkBanner.vue` — persistent lineage banner
- `components/reports/ForkedQueriesPanel.vue` — grouped inherited queries panel

**Files to modify:**
- `pages/reports/[id]/index.vue` — detect forked report, render banner + panel + separator
- Summary completion rendering (detect `is_fork_summary`, render as special card)

### Phase 6: Frontend — Artifact in Forked Report
- `ArtifactFrame.vue` already works if queries/visualizations are properly owned by the new report
- No changes needed — the existing `/api/queries?report_id=NEW&artifact_id=NEW` flow resolves correctly because we remapped visualization_ids in step 5

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
