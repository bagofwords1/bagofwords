---
key: dashboard-theme
title: Company dashboard theme
description: Use when building or restructuring a dashboard — the company's rules for what goes on it, in what order, and how it looks.
category: dashboard
version: "1.1"
modes: [chat, training]
tags: [dashboard, design]
---

The component mechanics (KPICard, SectionCard, DataTable, EChart, dark mode,
`data-bow-viz` provenance) are already specified for you elsewhere. This skill
is about the decisions those mechanics cannot make: **what belongs on the
dashboard, in what order, and what the reader is meant to do with it.**

> **This skill is meant to be edited.** The sections below are deliberately
> generic; replace them with your organization's actual conventions — palette,
> logo, standard KPI set, required footer, naming — and every dashboard the
> agent builds will follow them. Until you do, it enforces sound defaults.

## 1. Decide the dashboard's job first

Three kinds, and they look different. Pick one and say which you picked:

- **Monitoring** — "is anything wrong right now?" Few numbers, each with a
  comparison and a threshold. Optimized for a five-second glance. If a tile
  cannot be acted on when it turns red, it does not belong.
- **Exploration** — "let me slice this myself." Filters and cross-filtering
  matter more than the default view. Tables earn their place here.
- **Narrative** — "here is what happened and why." Ordered top to bottom,
  reads like an argument. Often better as a doc than a dashboard.

Building a monitoring dashboard with exploration's density is the most common
failure: 20 tiles, none of which mean anything without study.

## 2. Order by decision, not by data source

Top of the page answers the headline question. Everything below it explains or
qualifies that answer. Concretely:

1. **Headline row** — 3 to 5 KPIs, no more. Each with a comparison (vs. prior
   period or vs. target). A number with no comparison is not information.
2. **The one chart that explains the headline** — usually a trend.
3. **Breakdowns** — the dimensions that decompose the headline.
4. **Detail table** — last, for the reader who wants rows.

Anything that does not support the headline question belongs on a different
dashboard. Say so rather than adding it.

## 3. Rules that survive review

- **Every number is comparable.** Absolute value, plus delta and direction.
  State the comparison window in the label, not in a tooltip.
- **Every number is inspectable.** Wire the `viz` prop (or `data-bow-viz` on
  custom markup) on every tile, chart and table, and add the calculation
  formula where the value is derived. A number a reader cannot trace is a
  number they will not trust.
- **Format for the reader, not the database.** Currency with a symbol and
  sensible precision; large numbers abbreviated (1.2M, not 1204338); percents
  with one decimal at most; dates in the org's convention. Never show a raw
  timestamp where a date will do.
- **Null is a value.** Decide and show whether it means zero, missing, or
  not-applicable — never let it render as an empty cell that reads as zero.
- **Say what the data does not cover.** A partial current period, an excluded
  region, a source that lags a day — put it in a caption on the dashboard, not
  only in the chat reply. The dashboard outlives the conversation.
- **Chart choice follows the question**: trend over time → line; composition
  at one moment → stacked bar or treemap, not a pie beyond ~5 slices;
  comparison across categories → horizontal bar sorted by value; relationship →
  scatter. Never a dual axis unless both series share units.

## 4. House style

Replace this section with your organization's conventions. Until you do, the
defaults are: the product's built-in components and theme, no custom palette,
titles as sentence case, and no logo.

## 5. Before you call it done

Re-read the dashboard as the person who will open it Monday morning:

- Can they tell in five seconds whether things are good or bad?
- Does every tile have a comparison, a trace to its data, and a reason to exist?
- Is there anything on it that they cannot act on? Remove it or move it.
