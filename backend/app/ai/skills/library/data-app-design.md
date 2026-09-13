---
key: data-app-design
title: Data app design
description: Use when building or redesigning a connected data app, including dashboards and exploration tools.
category: dashboard
version: "1.0"
order: 25
default_enabled: true
modes: [chat, training]
tags: [data-app, dashboard, design]
---

Design the working interface around the user's task: exploring records, comparing
results, inspecting details, or monitoring performance. A dashboard is one view.
Start with a useful working surface and a stable app title. Summary metrics,
hero charts, cards, tabs, and themes are optional; do not add them to fill space.

Use a coherent type scale, readable numerals, aligned controls, useful density,
intentional whitespace, and consistent chart colors. Custom React/CSS and the
built-in kit are both supported. Respect the organization's actual brand.

Connect backend query controls through useParams and useParamOptions. Separate
query parameters from snapshot filtering and local selection/navigation state.
Show loading, errors, empty results, and valid selected states. Preserve valid
selection across query updates and explain when it leaves the result.

Every metric/chart/table needs source provenance; derived values need honest
units, grain, and coverage. Never invent comparisons or totals from partial data.
Only expose actions with implemented behavior and available backend support.

For monitoring requests, use meaningful summary metrics, relevant comparisons,
and compact status/trend views. For catalogs, let search and records lead. For
analysis, give controls and the main chart sufficient space. Do not impose one
composition on every task.

On ordinary edits, preserve the existing dashboard's design and runtime contract.
Organization administrators may customize these conventions; preserve their edits.
