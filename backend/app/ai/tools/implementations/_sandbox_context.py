"""
Shared sandbox runtime context for artifact tools.

Single source of truth describing the iframe sandbox environment where
LLM-generated artifact code executes. Used by create_artifact, edit_artifact,
and read_artifact to ensure the AI never misdiagnoses missing globals or
misinterprets minified React errors.
"""

# ---------------------------------------------------------------------------
# Prompt section: embedded in LLM prompts for create_artifact & edit_artifact
# ---------------------------------------------------------------------------

SANDBOX_RUNTIME_PROMPT = """
═══════════════════════════════════════════════════════════════════════════════
SANDBOX RUNTIME ENVIRONMENT (pre-loaded globally — do NOT import or redefine)
═══════════════════════════════════════════════════════════════════════════════

The generated code runs inside a sandboxed iframe. The following libraries and
helpers are **already loaded globally** — do NOT import, redefine, or remove
references to any of them:

• **React 18** — `React`, `ReactDOM` available globally
  - Use hooks: useState, useEffect, useRef, useMemo, useCallback

• **Recharts** — All components are pre-loaded as globals (do NOT destructure or import)
  - Available globally: BarChart, Bar, LineChart, Line, AreaChart, Area, PieChart, Pie, Cell, ScatterChart, Scatter, RadarChart, Radar, PolarGrid, PolarAngleAxis, PolarRadiusAxis, Treemap, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer, Label, LabelList, ComposedChart
  - Declarative & composable — no imperative init/dispose/resize needed
  - Always wrap charts in `<ResponsiveContainer width="100%" height={400}>`

• **ECharts 5** — `echarts` available globally (kept for backward compatibility)
  - For NEW dashboards, prefer Recharts (declarative, simpler, fewer bugs)
  - ECharts is available if you need exotic chart types not in Recharts

• **Tailwind CSS (v3.4)** — All utility classes available
  - Use modern design: rounded-xl, shadow-lg, backdrop-blur, gradients
  - Dark/light themes, responsive grids, flexbox

• **Babel** — JSX is transpiled automatically
  - Code must be wrapped in `<script type="text/babel">...</script>`

• **useArtifactData()** — Global React hook
  - Returns `null` while data is loading, then `{ report, visualizations }`
  - `report`: `{ id, title, theme }`
  - `visualizations`: array of `{ id, title, view, rows, columns, dataModel }`
  - Always handle the `null` (loading) state before accessing data

• **Pre-built UI components** — all global, do NOT redefine:
  - `<LoadingSpinner size={24} className="" />` — animated spinner
  - `<CustomTooltip />` — dark styled Recharts tooltip. Use: `<Tooltip content={<CustomTooltip />} />`
  - `<KPICard title="" value="" subtitle="" color="#3B82F6" className="" />` — stat card with accent bar
  - `<SectionCard title="" subtitle="" className="">...children...</SectionCard>` — white card wrapper
  - `fmt(n, {currency: true})` — number formatter (currency, pct, auto K/M/B)

• **window.ARTIFACT_DATA** — Raw data object (same shape as useArtifactData return)

The code is rendered into `<div id="root">`.
""".strip()


# ---------------------------------------------------------------------------
# Observation field: included in read_artifact observations for the planner
# ---------------------------------------------------------------------------

SANDBOX_RUNTIME_OBSERVATION = (
    "This code runs inside a sandboxed iframe that pre-loads these globals — "
    "do NOT redefine, import, or remove references to them: "
    "React (v18), ReactDOM, Recharts (declarative React charting via window.Recharts), "
    "echarts (v5, for backward compat), Tailwind CSS (v3.4), Babel (JSX transpilation), "
    "useArtifactData() hook (returns { report, visualizations } or null while loading), "
    "Pre-built globals (do NOT redefine): LoadingSpinner, CustomTooltip, KPICard, SectionCard, fmt(). "
    "For new dashboards, prefer Recharts over raw ECharts — it is declarative and composable. "
    "The code is wrapped in <script type='text/babel'> and rendered into <div id='root'>. "
    "All globals (React, Recharts, echarts, LoadingSpinner, useArtifactData) are always available at runtime."
)
