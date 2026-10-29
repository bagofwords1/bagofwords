---
key: demo-data
title: Create a demo dataset
description: Use when an admin asks for demo/mock/sample data for a domain — design a realistic schema, propose agents, and build it with create_demo_dataset.
category: data_modeling
version: "1.0"
order: 80
default_enabled: true
modes: [training]
requires_setting: enable_demo_data_generation
tags: [demo-data, onboarding, agents]
---

The admin wants a realistic, fictional database to demo or prototype agents
on ("a demo for finance in e-commerce", "HR data", "app logs and metrics",
"machine sensor data"). You design it; `create_demo_dataset` shows it to them
on a card, they tick the agents they want and approve, then the data is
generated table by table and the connection and agents are created.

Your spec is everything: the generator only knows what your table
descriptions, column descriptions, `generation_hint`s and `realism_notes`
say. Vague spec → flat, uniform, obviously-fake data.

## 1. Decide the shape (do not ask unless the domain is truly unclear)

Pick a concrete fictional business and size from the ask ("a mid-size online
retailer, ~5k customers, 2 years"). Break the domain into archetypes — most
real asks mix several:

| Archetype | Typical tables | What makes it realistic |
|---|---|---|
| Transactional (sales, finance, e-commerce, procurement) | customers, products, orders, order_items, payments, refunds, invoices | FK chain parent→child, totals computed from lines, seasonality (Q4, weekdays), long-tail customers, refunds/returns/chargebacks at small rates |
| Entities with history (HR, CRM, inventory) | employees, departments, positions, salary_history, accounts, opportunities | effective-dated rows (start/end), status lifecycles, hierarchies (manager_id self-reference), attrition/churn |
| Event / log streams (app logs, audit, clickstream, tickets) | services, log_events, requests, sessions, tickets | irregular sorted timestamps, severity mix (INFO ≫ WARN > ERROR), message templates with variables, trace/session ids, bursts |
| Metrics / time series (monitoring, KPIs) | hosts, services, metrics (one row per entity per interval) | regular intervals, daily/weekly cycles, slow drift, injected incidents (spike → recovery) correlated with error logs |
| Machine / IoT (sensors, manufacturing, fleet) | machines, sensors, readings, maintenance_events, failures | many devices × frequency, correlated signals (temp ↑ → vibration ↑ → failure), downtime gaps, maintenance resets |

## 2. Write the spec

- **3–8 tables** for a demo; up to 15 when the domain needs it. snake_case names.
- **Every table**: a `description` that states the grain ("one row per order
  line"), one integer or prefixed-string `primary_key`, realistic `row_count`
  (dimensions small: 5–500; facts larger: 1k–100k; metrics can reach
  ~200k — stay under 1M rows total).
- **Foreign keys** as `references: "parent_table.parent_pk"`. No cycles;
  self-references (manager_id → employees.employee_id) are fine.
- **Column descriptions** are business meaning — they become the catalog the
  agents read. **`generation_hint`** says how values behave: distributions
  ("lognormal median $45"), mixes ("card 70% / paypal 20% / bnpl 10%"),
  formulas ("= quantity * unit_price - discount"), ordering ("after
  order_date by 0–5 days"), templates ("'User {id} login failed from {ip}'").
- **`realism_notes`** carry cross-table rules: "refunds only on delivered
  orders, ~3%", "Q4 revenue +40%", "2 incidents in the last 30 days: p95
  latency > 2s for 30–90 min with ERROR bursts on the same service".
- **Dates**: `date_range_end` is today or earlier; 1–3 years of history is
  usual. Mark due/expected/scheduled dates explicitly in the column name or
  description — anything else in the future is rejected.
- **Fictional only**: no real people or companies, no valid card numbers,
  national ids or real emails. Public reference data (countries, currencies,
  ISO codes) is fine.
- Pick one emoji `icon` for the dataset.

## 3. Suggest 1–4 agents

Each agent is a focused analyst on a subset of tables — split by the
questions people ask, not one agent per table. Give each:
- a clear `name` and one-line `description`,
- ONE emoji `icon` matching its focus (💰 revenue, 👥 people, 🚨 incidents,
  📦 inventory, 🏭 production, 📈 growth...),
- `tables` it should see (shared dimensions can appear in several agents),
- 3–4 `conversation_starters` answerable from the data you designed,
- 2–5 `instructions`: business definitions and rules that match your hints
  ("Net revenue = sum(order_items.amount) - refunds.amount", "An incident is
  p95_latency_ms > 2000 for 5+ consecutive minutes").

Leave `selected: false` on a niche agent you think is optional.

## 4. Call `create_demo_dataset`, then follow through

- The user reviews the card. **Rejected with feedback** → revise exactly what
  they asked and call again (keep everything else). **Invalid spec** → fix the
  listed errors and call again. **Failed table** → simplify that table's
  hints/row count and call again.
- **Created** → the agents are attached to this session. Summarize in 2–3
  lines what was built, then offer next steps: try a starter question, review
  the instructions, or create evals (create-evals skill).
- Do not call `create_agent` for the same agents again — they already exist.
