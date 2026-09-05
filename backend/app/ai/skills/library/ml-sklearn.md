---
key: ml-sklearn
title: Predictive modeling with scikit-learn
description: Use when asked to predict, score, forecast, segment or find drivers — frame the problem and validate honestly before reporting.
category: code_gen
version: "1.0"
tags: [machine-learning, modeling]
---

# Predictive modeling with scikit-learn

The sandbox can train models, which makes it easy to produce a number that
looks like a prediction and is worthless. The discipline below is what makes
the difference.

## What is actually available

- **scikit-learn and scipy** — subject to the org's machine-learning setting.
  When it is off, the sandbox rejects those imports: say plainly that training
  is disabled in AI Settings and offer the pandas/numpy alternative (target
  rate by feature bucket, correlations, a fitted trend line). Never hand-roll
  an estimator in numpy to work around the switch.
- **NOT available**: statsmodels, prophet, xgboost, shap. Do not import them
  and do not promise what they would provide (no p-values from statsmodels, no
  SHAP values). Say what you used instead.
- **Nothing persists.** The estimator is never saved — every rerun retrains
  from scratch. Set `random_state` on everything that takes it, or the
  dashboard changes numbers on refresh for no reason.
- **Row limits.** Sample down to the org's training row limit before fitting.
  Sample randomly, with a fixed seed — never `LIMIT n` on an ordered query,
  which silently trains on the oldest or largest rows only.
- Results must come back as a **tidy DataFrame** (`feature`/`importance`,
  `metric`/`value`, `actual`/`predicted`), not an object.

## 1. Frame the problem before touching the data

Write down, and confirm with the user when it is not obvious:

- **The target.** What exactly is being predicted, measured how, over what
  horizon. "Churn" is not a target; "no order in the 90 days after the
  snapshot date" is.
- **The unit of observation.** One row per customer? Per customer-month?
- **When the prediction is made.** This is the crux, and it defines what
  features are legal (see leakage).
- **What decision it feeds.** A model nobody acts on is not worth training.

If the answer to any of these is unclear, `clarify` — a well-fit model of the
wrong problem is worse than no model, because it is convincing.

## 2. Refuse when the data will not support it

Check before fitting, and say so plainly when it fails:

- **Too few rows or too few positives.** A few hundred rows, or a couple dozen
  positive cases, cannot support a classifier. Report the counts and offer
  descriptive analysis instead.
- **Severe class imbalance** (a small single-digit percent positive) — say it
  up front; accuracy will be meaningless and you must not report it.
- **No usable features** available at prediction time.

Declining here is the correct answer, not a failure to deliver.

## 3. Prevent leakage — the failure that produces 0.99

Any feature that could not have been known at prediction time will inflate the
score and make the model useless in production. Before fitting, check every
feature against the prediction moment:

- Fields updated **after** the outcome (a `closed_at`, a `churn_reason`, a
  final status).
- Aggregates computed over a window that **includes** the outcome period.
- IDs and near-duplicates of the target.
- For anything time-ordered, a **random split is leakage** — split by time:
  train on earlier, test on later.

**If your first score looks excellent, assume leakage and go find it.** Say
which features you excluded and why.

## 4. Baseline first, then the model

Always compute the trivial baseline — majority class for classification, the
mean or last value (or seasonal-naive, e.g. same weekday last week) for
regression and forecasting. Report the model *against* that baseline. A model
that barely beats the baseline should be reported as such, not dressed up.

Start with an interpretable model (logistic/linear regression, a small tree).
Go to an ensemble only when the simple model is clearly insufficient, and say
what the added complexity bought.

## 5. Report the metric that matches the decision

- **Never report accuracy on imbalanced data.** Use precision/recall (say which
  matters for this decision and why), ROC-AUC or PR-AUC.
- Regression: MAE or RMSE **in the units of the target**, next to the target's
  own scale. "RMSE 0.34" is unreadable without knowing the range.
- Always report the **test** metric, never the training one, and say how the
  split was made.
- Feature importances are **associations, not causes**. Report them as "the
  model relies on X", never "X causes Y". Say that correlated features split
  importance between them arbitrarily.

## 6. Deliver

Tidy outputs: a metrics table (with the baseline row), a feature-importance
table, and predictions where they are the point. When the user wants to switch
models from a dashboard, expose a `model_type`-style parameter.

Close with the honest limitations: sample size, the time window trained on,
what would invalidate the model, and when it should be retrained. A model
shipped without that paragraph will be over-trusted.
