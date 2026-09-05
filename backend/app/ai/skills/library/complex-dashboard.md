---
key: complex-dashboard
title: Complex parameterized dashboards
description: Use when building or editing a dashboard with filters or several linked queries — plan the parameters first, then build, then verify the wiring.
category: dashboard
version: "1.0"
modes: [chat]
tags: [dashboard, parameters, artifacts]
---

A dashboard with a filter is not a set of charts — it is a contract. Every
query the filter should drive has to declare it, the control has to bind to the
declared name, and the choices have to come from somewhere that does not
collapse when the filter is applied. Get any of those wrong and you ship a
dashboard where the filter moves some tiles and silently ignores others.

The planner's **Dashboard Contract preflight** already decides *which*
visualizations can participate in a cross-viz behavior, and what to do with the
ones that cannot (rebuild, drop, substitute, clarify). That classification is
not repeated here. This skill is about the **mechanism**: planning the query
set, declaring the parameters, and wiring them into the artifact.

## 1. Plan before the first tool call

Write the plan out and get it agreed before creating anything. Retrofitting a
filter contract costs a rebuild of every query it touches.

- **The question set.** What each tile answers. A tile that does not support
  the dashboard's headline question does not belong on it.
- **The filter contract.** Which dimensions and time windows the viewer will
  move. This is the decision everything downstream hangs on.
- **The query list**, one per distinct grain — *plus a dimension query for
  every filterable dimension*. Name these deliberately: an options source can
  reference a query by its exact title, so "Genres" is a better title than
  "Query 3".
- **Identity scoping.** Does each viewer see only their own rows? Decide now;
  it is a parameter source, not something the artifact can add later.
- **What is deliberately out of scope**, so an omission is not read as a bug.

State the plan in your reasoning before building. If two readings of the filter
contract would produce different dashboards, `clarify` — that question is far
cheaper now than after six queries exist.

## 2. Build the data — parameters are the filter mechanism

**Server-side parameters are how a dashboard filters. Not client-side row
filtering.** A parameter re-runs the query at the data source and fresh rows
arrive; filtering rows in the browser only hides what was already shipped.

On `create_data`, declare in `parameters`:

- **The same parameter, by the same `name`, on every query the filter must
  drive.** This is the rule that breaks dashboards when it is missed — the tile
  whose query never declared the parameter keeps showing unfiltered numbers,
  and it looks like a rendering bug rather than a missing declaration.
- **Real choices from a dimension query** — `options_source={query_id,
  value_column, label_column}`, where `query_id` may be that query's exact
  title or its id. Never derive the choice list from the filtered rows: it
  shrinks to whatever is currently selected. Use a static `options` list only
  for a genuinely small fixed set; omit choices only for free-form values
  (dates, search text).
- **`default: null` with `required: false` is the "All" path** — the generated
  code sees `None` and skips the predicate. Use it unless a dashboard genuinely
  must open on one selection.
- **Identity scoping is `source: 'identity'`** with an `identity_binding`
  (`viewer.email`, `viewer.profile_attributes.<attr>`, `viewer.groups`). The
  value is resolved server-side and locked to the viewer. Use
  `source: 'input_identity_default'` when it should merely *default* to the
  viewer's value and stay editable.
- **Do not speculatively parameterize.** A literal that nobody will move is
  just a literal. Parameters are for declared axes of variation.

Note each returned `viz_id` as you go — the artifact call needs them.

## 3. Build the artifact — bind to what was declared

Pass the complete source in `code` and the ordered `visualization_ids`. Then
the wiring, which is where this goes wrong:

- **`useParams()`** gives `{ declarations, values, pending, loading, error,
  setParam }`. `declarations` is the aggregate of what the queries actually
  declared, each with the `query_ids` it drives. **Render controls only for
  declared parameters** — a control for an undeclared name does nothing.
- **Every control must call `setParam('<exact declared name>', value)`.**
  Local React state alone never re-runs anything; the tile will look
  interactive and change nothing.
- **`useParamOptions(name)`** is the stable choice list. Bind
  `option.value` into `setParam` — **never the label**; values are usually ids
  and labels are display names. A `list` parameter needs a multi-select
  submitting an array; empty array = `null` = All.
- **Never render an input for an `identity` parameter.** Show a small "scoped
  to you" badge instead. `input_identity_default` params *are* editable.
- **Render `useParams().error`**, and show a subtle loading state while
  `loading` is true — a re-running query with no feedback reads as a frozen
  dashboard.
- **`useFilters` is a different tool**: it filters rows already in the browser.
  Legitimate for a quick client-side slice of one tile's rows; never a
  substitute for a parameter, and never the way to restrict data per viewer.
- **Every tile stays traceable**: `viz={vizById("<uuid>")}` on the built-in
  components, `data-bow-viz` / `data-bow-calc` on custom markup.

## 4. Editing an existing dashboard

`edit_artifact` is the edit path — exact find/replace ops against the current
code, mechanical and atomic. Size is never a reason to rebuild.

- **New data first.** If the edit needs data that does not exist in the right
  shape, `create_data` before the edit, then reference the new `viz_id`.
- **Adding a filter to existing queries → `add_parameter`, not a rebuild.** It
  adds one declared parameter to an existing query, rewriting only the
  filtering predicate (`(:name IS NULL OR col = :name)` for an optional
  parameter, `col IN :name` for a list), re-running once. **The query keeps its
  identity, so visualizations already bound to it pick the parameter up
  automatically.** Apply it to every query the new filter must drive.
- **Carry viz_ids forward.** The id list is a superset of what is on the canvas
  unless the user asked for a removal, or the contract preflight classified a
  tile as meaningless under the new filter.
- **Keep the title** unless a rename was asked for.

## 5. Verify the wiring before saying it is done

Walk these; each one is a dashboard that has shipped broken:

1. For every parameter, does **every** query that should respond declare it?
   The tile that does not is the failure nobody notices until a demo.
2. Does each control call `setParam` with the **exact declared name**?
3. Do the choices come from a dimension query, and do they stay complete when
   a filter is applied?
4. Does "All" work — an optional parameter left null returning unfiltered rows?
5. Are identity parameters absent from the controls and enforced server-side?
6. Do `loading` and `error` render?
7. Is every tile traceable to its query?

Then say what the dashboard filters by, which tiles respond, and — explicitly —
any tile that deliberately does not and why. A viewer who discovers that on
their own stops trusting the whole page.
