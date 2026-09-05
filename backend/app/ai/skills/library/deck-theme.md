---
key: deck-theme
title: Company deck theme
description: Use when asked for a deck, readout or presentation — the company's rules for how a deck is structured and what belongs on a slide.
category: dashboard
version: "1.1"
modes: [chat, training]
tags: [slides, communication]
---

The slide mechanics (the python-pptx authoring contract for `create_artifact`
with `mode='slides'`) are specified elsewhere. This skill is about the part that
makes a deck land: **what the slides say, in what order.**

> **This skill is meant to be edited.** Replace the structure and discipline
> below with your organization's actual deck conventions — template, title
> slide, colour and font rules, the standard sections leadership expects — and
> every deck follows them. Until you do, it enforces sound defaults.

## 1. Decide whether it should be a deck at all

- Read-and-discuss, no meeting → a doc reads better. Offer it.
- A dashboard someone will revisit → build the dashboard; the deck goes stale
  the moment it is saved.
- A meeting where someone must decide something → a deck. Continue.

## 2. Lead with the answer

Executives read the first slide and skim the rest. Structure accordingly:

1. **Title** — subject, period, owner, date.
2. **The answer, on one slide.** Three to five bullets that stand alone. If the
   audience read only this slide they should be able to act. Write this slide
   first; it is the deck's thesis, and everything after it is support.
3. **The numbers** — a KPI slide: each metric with its comparison and target.
4. **What moved and why** — one slide per driver, biggest first, each with the
   chart that proves it. If you cannot prove it, label it a hypothesis.
5. **Risks and what you do not know** — including data caveats. This slide is
   what makes the rest credible; do not drop it to save space.
6. **Recommendation / the decision being asked for**, with options and their
   trade-offs when there is a real choice.
7. **Appendix** — everything you wanted to include but could not justify above.

## 3. Slide discipline

- **One idea per slide.** The slide title states the finding as a sentence
  ("Enterprise churn drove the Q3 miss"), not a label ("Churn"). If the titles
  read in sequence tell the story, the deck works.
- **Six lines maximum** of body text. A paragraph on a slide is a doc in
  hiding.
- **One chart per slide**, with the takeaway written next to it. Never make the
  audience derive the point from the axes.
- **Every number carries its window.** "Revenue $4.2M" is unreadable; "Revenue
  $4.2M, Q3 2026, +8% QoQ" is a fact.
- **No number without provenance.** Build the data with `create_data` first,
  then author the deck from those results — never retype a number from memory
  or from a screenshot.

## 4. Before delivering

- Does slide 2 stand alone?
- Does every claim on a driver slide have a number, and does every number have
  a query behind it?
- Have you stated what would change your conclusion?
- Is anything on a slide that the audience cannot act on? Move it to the
  appendix.
