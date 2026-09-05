---
key: cohort-retention
title: Cohort and retention analysis
description: Use when asked about retention, churn over time, or how a group behaves after signup — build the cohort grid correctly.
category: general
version: "1.0"
tags: [analysis, retention]
---

# Cohort and retention analysis

Retention numbers are easy to compute and easy to compute wrong. Almost every
mistake is one of four: an incomplete final cohort, a moving denominator,
counting revisits as retention, or comparing cohorts at different ages.

## 1. Define the three axes explicitly, and state them

- **Cohort key** — what groups users: first purchase month, signup week,
  acquisition channel. Fixed at entry and never changes.
- **The activity that counts as retained** — logged in? purchased? used the
  core feature? "Active" must be one defined event, not a vibe.
- **Period grain and horizon** — day/week/month, and how many periods out.

Write these into the answer. A retention chart without its definition is
unreadable and non-comparable.

## 2. Build the grid

Rows are cohorts, columns are periods **since entry** (period 0, 1, 2, …), not
calendar dates. Period 0 is the cohort size and is the denominator for that
row — it never changes as later data arrives.

Non-negotiables:

- **Fixed denominator.** Retention in period N = users from the cohort active
  in period N ÷ cohort size. Dividing by "users still around" produces a
  number that only goes up and means nothing.
- **Drop incomplete cells.** A cohort that is three months old has no month-6
  cell. Leave it empty — never zero, never averaged in. An incomplete final
  cohort plotted as a collapse is the most common false alarm in this analysis.
- **Compare at equal age.** Cohort A at month 3 vs. cohort B at month 3. Never
  compare a mature cohort's month 12 to a young cohort's month 1.
- **Decide the revisit rule.** Does period-3 retention require activity *in*
  period 3, or activity in period 3 *or later*? Both are legitimate; they give
  different curves. State which you used.

## 3. Read it properly

- The **shape** matters more than any single number: a curve that flattens
  means a real retained base; one that keeps declining means no floor yet.
- Look **down** columns (is retention improving for newer cohorts?) as well as
  **across** rows.
- Cohort size varies — a tiny cohort's percentage is noise. Show the size next
  to the percentage, always.

## 4. Deliver

A heatmap or a set of curves, plus the cohort-size column, plus the
definitions from step 1. Call out the one or two cohorts that break the
pattern and, if the data supports it, what distinguished them. If it does not,
say the pattern is stable and stop — an invented explanation for cohort noise
is a real cost.
