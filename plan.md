# MCP Apps Integration Plan

Add MCP Apps support so `create_data` and `create_artifact` render interactive UIs inline in ChatGPT, Claude, and other MCP clients.

## Architecture Overview

```
Client (Claude/ChatGPT)
  │
  ├─ tools/list → sees _meta.ui.resourceUri on create_data & create_artifact
  ├─ resources/read → fetches HTML bundles (ui://bagofwords/visualization, ui://bagofwords/artifact)
  │
  ├─ tools/call create_data → returns result with visualization_id
  │   └─ Host renders ui://bagofwords/visualization in iframe
  │       └─ Iframe calls get_visualization (app-only tool) → gets full data from server
  │       └─ Renders chart/table/code tabs (like ToolWidgetPreview)
  │
  └─ tools/call create_artifact → returns result with artifact_id
      └─ Host renders ui://bagofwords/artifact in iframe
          └─ Iframe calls get_artifact_data (app-only tool) → gets artifact code + visualization data
          └─ Renders full dashboard (like artifact-sandbox)
```

Data always comes from the server via app-only tools, never from the LLM result.

---

## Step 1: Extend `MCPTool.to_schema()` to support `_meta`

**File:** `backend/app/ai/tools/mcp/base.py`

- Add optional `meta` property to `MCPTool` base class (returns `None` by default)
- Update `to_schema()` to include `_meta` in output when present:
  ```python
  def to_schema(self):
      schema = {"name": self.name, "description": self.description, "input_schema": self.input_schema}
      if self.meta:
          schema["_meta"] = self.meta
      return schema
  ```

## Step 2: Add `_meta.ui.resourceUri` to `create_data` and `create_artifact`

**Files:** `backend/app/ai/tools/mcp/create_data.py`, `backend/app/ai/tools/mcp/create_artifact.py`

- Override `meta` property on each tool:
  - `CreateDataMCPTool.meta` → `{"ui": {"resourceUri": "ui://bagofwords/visualization"}}`
  - `CreateArtifactMCPTool.meta` → `{"ui": {"resourceUri": "ui://bagofwords/artifact"}}`

## Step 3: Add app-only tools for data fetching

**New file:** `backend/app/ai/tools/mcp/app_tools.py`

Two new tools hidden from the LLM (`visibility: ["app"]`):

### `get_visualization`
- Input: `{ visualization_id: str }`
- Fetches: Query → default Step → Visualization
- Returns: `{ title, code, data: { rows, columns }, data_model, view, step_status }`
- Uses existing DB queries (same as what `ToolWidgetPreview` consumes via the completions API)

### `get_artifact_data`
- Input: `{ artifact_id: str }`
- Fetches: Artifact → content.code + content.visualization_ids → for each viz: Query → Step
- Returns: `{ report: { id, title }, code, visualizations: [{ id, title, view, rows, columns, dataModel, stepStatus }] }`
- Same data shape as `ArtifactFrame.vue` sends via `ARTIFACT_DATA` postMessage

**File:** `backend/app/ai/tools/mcp/__init__.py`
- Register both tools in `MCP_TOOLS` dict
- Add `visibility` property to `MCPTool` base (defaults to `["model", "app"]`)
- App-only tools override with `["app"]`

## Step 4: Update `tools/list` to include `_meta` and filter by visibility

**File:** `backend/app/routes/mcp.py`

- In `tools/list` handler, include `_meta` from `to_schema()` output
- Currently builds tool list as `{name, description, inputSchema}` — add `_meta` field when present

## Step 5: Declare capabilities for resources and the MCP Apps extension

**File:** `backend/app/routes/mcp.py`

- In `initialize` response, add:
  ```python
  "capabilities": {
      "tools": {},
      "resources": {},
  }
  ```
- Also declare the extension in server info or capabilities as appropriate for the spec

## Step 6: Handle `resources/read` and `resources/list` methods

**File:** `backend/app/routes/mcp.py`

Add handler for `resources/list`:
- Returns two UI resources:
  - `{ uri: "ui://bagofwords/visualization", name: "Visualization Viewer", mimeType: "text/html;profile=mcp-app" }`
  - `{ uri: "ui://bagofwords/artifact", name: "Artifact Viewer", mimeType: "text/html;profile=mcp-app" }`

Add handler for `resources/read`:
- For `ui://bagofwords/visualization` → serve the visualization HTML bundle
- For `ui://bagofwords/artifact` → serve the artifact HTML bundle
- Response format:
  ```python
  { "contents": [{ "uri": uri, "mimeType": "text/html;profile=mcp-app", "text": html_content }] }
  ```

## Step 7: Create visualization HTML bundle (for `create_data`)

**New file:** `frontend/public/mcp-visualization-app.html`

A self-contained HTML file that renders like ToolWidgetPreview:

- Uses `@modelcontextprotocol/ext-apps` SDK (inline or CDN)
- On connect: reads `tool-result` to get `visualization_id`
- Calls `tools/call get_visualization` to fetch full data from server
- Renders three tabs:
  - **Chart**: ECharts visualization based on `view.type` and `data_model`
  - **Data**: HTML table with rows/columns
  - **Code**: Python/SQL code display
- Bundles: ECharts (for charts), minimal CSS (Tailwind or custom)
- No React needed — this is a simpler single-visualization view
- Notifies host of size via `ui/notifications/size-changed`
- CSP metadata: `connectDomains` pointing to the Bag of Words API base URL

## Step 8: Create artifact HTML bundle (for `create_artifact`)

**New file:** `frontend/public/mcp-artifact-app.html`

Adapted from `artifact-sandbox.html`:

- Replace `ARTIFACT_READY`/`ARTIFACT_DATA` postMessage protocol with MCP Apps SDK
- On connect: reads `tool-result` to get `artifact_id`
- Calls `tools/call get_artifact_data` to fetch artifact code + visualization data
- Injects data as `window.ARTIFACT_DATA` (same shape the sandbox already expects)
- Evaluates `artifact.content.code` (the LLM-generated React/JSX) via Babel
- Bundles: React 18, Babel, ECharts 5, Tailwind CSS (same as artifact-sandbox.html)
- CSP metadata: `connectDomains` pointing to the Bag of Words API base URL

## Step 9: Serve HTML bundles from backend

**File:** `backend/app/routes/mcp.py` (or new helper)

- Load HTML files from `frontend/public/mcp-visualization-app.html` and `frontend/public/mcp-artifact-app.html`
- Optionally inject the API base URL into the HTML at serve time (so the app knows where to call back)
- Cache the loaded HTML in memory

## Step 10: Update protocol version

**File:** `backend/app/routes/mcp.py`

- Update `MCP_PROTOCOL_VERSION` if needed to match MCP Apps spec requirements
- Ensure the `initialize` response advertises the `io.modelcontextprotocol/ui` extension

---

## File Change Summary

| File | Change |
|------|--------|
| `backend/app/ai/tools/mcp/base.py` | Add `meta` and `visibility` properties, update `to_schema()` |
| `backend/app/ai/tools/mcp/create_data.py` | Add `meta` with `ui.resourceUri` |
| `backend/app/ai/tools/mcp/create_artifact.py` | Add `meta` with `ui.resourceUri` |
| `backend/app/ai/tools/mcp/app_tools.py` | **New** — `get_visualization` and `get_artifact_data` app-only tools |
| `backend/app/ai/tools/mcp/__init__.py` | Register app-only tools |
| `backend/app/routes/mcp.py` | Add `_meta` to tools/list, add `resources/list` + `resources/read`, update capabilities |
| `frontend/public/mcp-visualization-app.html` | **New** — ToolWidgetPreview-like MCP App |
| `frontend/public/mcp-artifact-app.html` | **New** — Artifact dashboard MCP App |
