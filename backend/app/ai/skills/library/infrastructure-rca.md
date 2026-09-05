---
key: infrastructure-rca
title: Infrastructure root cause analysis
description: Use when something broke or degraded in the infrastructure — find the event, build a timeline from logs, metrics and alerts, then separate cause from symptom.
category: general
version: "1.0"
modes: [chat]
tags: [observability, diagnostics]
---

An incident produces far more signal than explanation. Dozens of alerts fire,
every saturation metric looks bad, and the loudest symptom is almost never the
cause. This is the order that gets to an answer instead of a story.

Keep a note (`create_note`, then `edit_note` as you go) holding the timeline,
the hypotheses, and what each one is ruled in or out by. An RCA that lives only
in your head loses its own evidence halfway through.

## What you can actually query

Observability connections appear as ordinary tables — inspect them with
`describe_tables` and query them with `create_data`. What is on offer depends
on what is connected:

- **Zabbix** — `problems` and `triggers` (what is firing), `events` (the
  timeline), `hosts` / `host_groups` (what it runs on), `items` (what is
  measured), `history` (raw samples) and `trends` (hourly aggregates).
- **AppDynamics** — `health_rule_violations` and `events`,
  `business_transactions` and `service_flows` (the call graph),
  `metric_data`, `applications` / `tiers` / `nodes`, and `snapshots`.
- **Aria Operations** — `alerts` with `symptoms` and `contributing_symptoms`
  (its own cause/symptom split, worth reading before you build your own),
  `resources` and `relationships` (topology), `metrics` / `metrics_latest` /
  `metrics_topn`.
- **Splunk** — SPL passthrough plus the org's saved searches and dashboards;
  those encode what the team already knows to look at.
- **Prometheus** — one table per metric, with the label set as columns plus a
  timestamp and value.
- **Jaeger** — `services`, `operations`, spans, and `dependencies`
  (parent/child call counts) for where latency actually went.
- **Elasticsearch / OpenSearch** — log indices.
- **ServiceNow** — incidents and, importantly, change records.

Never assume a source exists. Check, and say plainly which of logs, metrics,
traces and change records you had — an RCA missing one of them is a different
claim than one that had all four.

## 1. Pin the incident before investigating it

Write down, and confirm if it is not stated: **what** broke (the user-visible
symptom, not the alert name), **when** it started and whether it is still
happening, and **who/what is affected** — which service, region, tenant, host
group. Convert every window to one timezone and say which; sources disagree,
and a UTC-vs-local mix-up invents causality that is not there.

If the "incident" turns out to be inside normal variation — compare against the
same window on previous days — say so and stop. Explaining noise as an incident
costs a team a day.

## 2. Find the event, do not guess it

Start from what fired: `problems` / `health_rule_violations` / `alerts` over a
window that starts **well before** the reported symptom. Two things matter more
than severity:

- **First-seen order.** Sort ascending by start time. In an alert storm the
  cause usually fires first and quietly; the severe ones are downstream.
- **What stopped firing.** An alert that cleared mid-incident is a strong hint
  about sequencing.

Then widen once: was anything already degraded *before* the first alert? Alerts
have thresholds, so the metric moved earlier than the alert did. Find the first
moment the metric left its normal band, not the first moment someone was paged.

## 3. Build the timeline

One ordered list, in the note, with a source against each entry: first metric
deviation, first alert, dependent alerts, error-rate change in logs, latency
change in traces, any deploy or config change, first user report, mitigation,
recovery.

For metrics, use the aggregate table for the wide view and the raw one to zoom
(`trends` before `history` in Zabbix; a coarse step before a fine one in
Prometheus). Pulling raw samples across a multi-day window returns enormous
result sets and tells you less than the aggregate would.

The shape of the timeline is the finding. A gradual ramp is saturation or a
leak; a step change is a deploy, a config push, or a dependency failing; a
sawtooth is a restart loop.

## 4. Localize it

Narrow to the smallest set of hosts, nodes, tiers or services that shows the
problem, and check what they share — a rack, an availability zone, a version, a
config, an upstream dependency. **What is unaffected is as informative as what
is affected**: if only one tier degraded, everything shared by all tiers is
ruled out.

Use topology rather than inference where you have it: Aria `relationships`,
AppDynamics `service_flows`, Jaeger `dependencies`. Follow the call graph
downstream to the first thing that was slow, and check whether *its* dependency
was slow before it.

## 5. Correlate with change — carefully

Most infrastructure incidents are caused by a change. Look for deploys, config
pushes, feature flags, scaling actions, certificate expiries, scheduled jobs,
and ServiceNow change records in the window.

Then rule out. A change is not the cause if it **predates the first deviation**,
or if it landed on hosts that did **not** degrade. State the ruled-out changes —
half the value of an RCA is the list of things it is safely not.

## 6. Separate cause from symptom

Saturation cascades: a slow database fills connection pools, which spikes
latency, which trips a circuit breaker, which sheds load, which drops CPU.
Every one of those alerts. Test each candidate:

- **Timing.** Did it move before the thing it supposedly caused?
- **Mechanism.** Can you name how A produces B? "CPU was high" is not a
  mechanism.
- **Scope.** Does the affected set match?
- **Counter-example.** Was there a host with the same condition that stayed
  healthy? If yes, it is not sufficient on its own.

Where the platform already did this work — Aria's `contributing_symptoms` under
an alert — read it before proposing your own chain, and say where you agree.

Resource exhaustion needs its own check: disk, inodes, file descriptors,
connection pools, thread pools, queue depth, memory including cache pressure.
These fail suddenly after a long silent ramp, so the trigger and the cause can be
days apart.

## 7. Report

Deliver with `create_doc` when this will be read by anyone but the asker.
In this order:

1. **What happened** — user-visible impact, precise start and end, blast radius.
2. **Root cause** — one sentence, then the mechanism in two or three.
3. **Timeline** — the ordered table, each row with its source.
4. **Evidence** — the queries and charts behind each claim; `create_data`
   first, then embed the visualization in the doc.
5. **Ruled out** — the hypotheses you tested and what killed each one.
6. **What you could not see** — missing sources, retention gaps, sampled
   traces, hosts not monitored. This bounds the conclusion honestly.
7. **Confidence**, stated plainly, and what evidence would raise it.
8. **Actions** — the fix, and separately the detection gap: if the first signal
   was a user complaint rather than an alert, that is a finding in its own right.

A causal chain diagram earns its place here — a ```mermaid flowchart from
trigger to user impact makes the mechanism checkable at a glance.

If the evidence does not converge, say so: give the timeline, the ranked
hypotheses with what each would predict, and the one query or log that would
settle it. A named unknown is a useful RCA. A confident guess is not.
