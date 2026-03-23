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
  - Create beautiful, reusable components

• **ECharts 5** — `echarts` available globally
  - Full charting library: bar, line, area, pie, scatter, heatmap, radar, treemap, sunburst, gauge, funnel, sankey, etc.
  - Rich animations, tooltips, legends, gradients
  - Responsive with chart.resize()
  - Always init in useEffect, dispose on cleanup, handle window resize

• **Tailwind CSS (v3.4)** — All utility classes available
  - Use modern design: rounded-xl, shadow-lg, backdrop-blur, gradients
  - Dark/light themes, responsive grids, flexbox
  - Animations: animate-pulse, transition-all, hover effects

• **Babel** — JSX is transpiled automatically
  - Code must be wrapped in `<script type="text/babel">...</script>`

• **useArtifactData()** — Global React hook
  - Returns `null` while data is loading, then `{ report, visualizations }`
  - `report`: `{ id, title, theme }`
  - `visualizations`: array of `{ id, title, view, rows, columns, dataModel }`
  - Always handle the `null` (loading) state before accessing data

• **LoadingSpinner** — Global React component
  - Props: `size` (number, default 24), `className` (string)
  - Inherits text color via currentColor
  - Use for loading states instead of building your own spinner

• **window.ARTIFACT_DATA** — Raw data object (same shape as useArtifactData return)
• **window.ARTIFACT_READY** — Boolean flag set when iframe is initialized

The code is rendered into `<div id="root">`.

═══════════════════════════════════════════════════════════════════════════════
REACT ERROR CODES — the sandbox uses development React (readable errors)
═══════════════════════════════════════════════════════════════════════════════

Development React provides full error messages. If you still encounter
"Minified React error #NNN" (e.g. in cached builds), these are the common codes:

• **#130** — Component returned `undefined` from render. Check that all
  components return valid JSX (not `undefined` or missing return).
• **#152** — Hook called outside a component body or in a conditional. Ensure
  all useState/useEffect/etc. calls are at the top level of a function component.
• **#185** — Rendered fewer hooks than expected. A hook is inside an `if`/`return`
  that skips it on some renders. Move hooks above early returns.
• **#301** — `ReactDOM.createRoot` called on a container that already has a root.
  Ensure `createRoot` is called only once.
• **#310** — Rendered an invalid React element — typically passing a plain object
  or array where React expects a string, number, or component. Check that you
  are not accidentally rendering `{someObject}` instead of `{someObject.value}`.
  This is NOT about missing imports or undefined components.
• **#418** / **#423** — Hydration mismatch (server vs client). In artifact
  context this usually means the initial render differs from a re-render.
• **#31** — Objects are not valid as a React child. If you need to display an
  object, serialize it with `JSON.stringify()` or extract a scalar field.

When diagnosing errors, focus on the actual code logic — not on whether globals
like React, echarts, LoadingSpinner, or useArtifactData are defined (they always are).
""".strip()


# ---------------------------------------------------------------------------
# Observation field: included in read_artifact observations for the planner
# ---------------------------------------------------------------------------

SANDBOX_RUNTIME_OBSERVATION = (
    "This code runs inside a sandboxed iframe that pre-loads these globals — "
    "do NOT redefine, import, or remove references to them: "
    "React (v18), ReactDOM, echarts (v5), Tailwind CSS (v3.4), Babel (JSX transpilation), "
    "useArtifactData() hook (returns { report, visualizations } or null while loading), "
    "LoadingSpinner component (props: size, className), "
    "window.ARTIFACT_DATA, window.ARTIFACT_READY. "
    "The code is wrapped in <script type='text/babel'> and rendered into <div id='root'>. "
    "Development React is used — error messages are readable. Minified error codes like #310 mean 'invalid React child' "
    "(object rendered instead of string/number), NOT missing imports. "
    "All globals (React, echarts, LoadingSpinner, useArtifactData) are always available at runtime."
)
