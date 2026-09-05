---
key: alert-monitor-design
title: Design a metric alert or scheduled monitor
description: Use when asked to be told when something breaks — turn it into a scheduled check with a threshold, a window and a channel.
category: general
version: "1.0"
tags: [monitoring, automation]
---

"Tell me when revenue drops" is not a specification. An alert built from it
either never fires or fires every day, and both outcomes end the same way — it
gets muted, and the real incident is missed. Nail down five things before
scheduling anything.

## 1. The five decisions

1. **The condition, as a query that returns a comparable number.** Not "drops"
   — "daily net revenue is below X" or "below 80% of the trailing 7-day median".
2. **The threshold, chosen from history, not from intuition.** Run the metric
   over the last several months and see how often the proposed threshold would
   have fired. If it would have fired 40 times, it is wrong. If it never fired,
   it will not catch the next incident either. Show the user this backtest
   before scheduling — it is the step that makes the alert usable, and it takes
   one query.
3. **The comparison window.** Same day last week beats yesterday for anything
   with weekly seasonality; a trailing median beats a trailing mean when
   outliers are common.
4. **The schedule.** Run it after the data is expected to land, not at
   midnight — and check freshness inside the alert (see below).
5. **Who is told, and where.** Match the channel to the urgency: a chat channel
   for something needing attention today, email for a daily digest.

## 2. Build resilience into the check itself

- **Distinguish "bad" from "missing".** A metric that reads zero because the
  pipeline did not run must alert differently from a real drop to zero — check
  freshness and row count inside the monitor and say which condition fired.
  This is the single highest-value thing to get right.
- **Require persistence** for noisy metrics: fire on two consecutive breaches
  rather than one, or on a rolling average.
- **Suppress repeats.** An alert that fires every run for the same ongoing
  incident trains people to ignore it. Fire on state *change* where you can.
- **Know when it should not fire** — known maintenance windows, month-end
  spikes, holidays.

## 3. Write an alert someone can act on

The message must carry, in this order: what fired, the actual value vs. the
threshold, the comparison window, the direction and size of the change, and a
link to the report or dashboard for the detail. An alert that says only
"revenue anomaly detected" costs the reader ten minutes before they know
whether to care.

Keep it short enough to read on a phone.

## 4. Schedule and confirm

Create the scheduled task, then tell the user in plain words what will happen:
the exact condition, when it runs, who gets told, how often it would have fired
historically, and how to change or cancel it. Never leave someone guessing what
you just put in place on their behalf.
