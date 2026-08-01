---
key: slides-creator
name: Slides Creator
description: Read before building any slide deck or presentation — storyline, takeaway titles, density and layout rules.
category: visualization
version: 1.0.0
icon: i-heroicons-presentation-chart-bar
---

# Slides Creator

How to turn analysis into a deck people can follow without you narrating it.
Use this whenever you create or rebuild a presentation (`create_artifact` with
`mode: "slides"`). It covers what to say and in what order; the tool prompt
covers the python-pptx mechanics.

## Start with the storyline, not the slides

Before writing any code, decide the argument in one sentence: what should the
audience believe or do after seeing this? Every slide either supports that
sentence or gets cut.

Then order the slides so each one earns the next. The default arc that works
for analytical decks:

1. **Title** — subject, audience, date.
2. **The headline** — the single most important finding, stated outright. Do
   not save the conclusion for the end; executives read the first two slides.
3. **Evidence** — one slide per supporting point, each with a chart.
4. **What changed / why** — drivers, segments, root cause.
5. **So what** — implications, risks, recommended actions.
6. **Appendix** — detail, methodology, caveats.

For a status or review deck, replace 2–5 with: where we are → what moved →
what's blocked → what's next.

## Titles carry the message

The title is the one line everyone reads. Make it the finding, not the topic.

- Weak: "Revenue by Region"
- Strong: "EMEA drove all of Q3 growth; every other region was flat"

A reader should be able to page through titles alone and get the whole
argument. If a title could sit on any deck in any quarter, it is a label, not
a takeaway. Keep titles under about twelve words so they fit on one line.

## One idea per slide

If a slide needs the word "and" twice to explain, split it. Density limits
that hold up in practice:

- One chart per slide, unless two charts are being directly compared.
- At most five bullets; at most two lines each; no sub-bullets.
- No paragraphs. If prose is needed, the deliverable is a document, not a deck.
- Numbers get units and periods ("$4.2M, Q3" not "4200000").

Every slide should carry a visual — a chart, a KPI figure, a diagram. A slide
of nothing but bullets is a slide nobody remembers.

## Use the real data

Reference visualizations that already exist in the report by their
`viz_id` rather than re-describing numbers in text; the chart and its data stay
linked and stay correct. When you state a figure in a title or takeaway, it
must match what the chart shows. Never invent a number to make a point land —
if the data does not support the claim, change the claim.

If a deck needs a chart that does not exist yet, create the data first, then
build the deck referencing it.

Not every slide needs a chart from the data: title slides, section dividers,
agendas, and recommendation slides are legitimately text and shapes only.

## Design that reads as deliberate

Pick a palette and hold it for the whole deck. One color should dominate,
with one supporting tone and one accent used sparingly for emphasis — never
three colors competing at equal weight. Dark title and closing slides with
lighter content slides in between gives a deck structure you can feel.

Consistency is what makes a deck look designed:

- The same margin on every slide. Nothing within half an inch of the edge.
- Titles starting at the same position on every content slide.
- One type scale — title, subtitle, body, caption — reused everywhere.
- Charts sized to the same content area, not fit to whatever space is left.

Vary the layout between slides — full-bleed chart, chart with a takeaway
panel, KPI row, two-up comparison, quote or callout — so the deck does not
read as the same template twelve times. Avoid the tells of generic decks:
thin accent lines under every title, drop shadows on everything, clip-art
icons, and gradients used as decoration.

## Speaker notes

Put the supporting detail the slide does not need — caveats, method, the
number behind the number — in speaker notes rather than shrinking body text
to fit. A slide that has to be read aloud verbatim is over-written.

## Before you finish

Check the rendered deck, not the code that generated it:

- Does any text overflow its box or run off the slide? This is the most
  common defect and it is always visible to the audience.
- Do any elements overlap or collide?
- Is anything too close to a slide edge, or are gaps uneven between slides?
- Is every figure readable — no low-contrast text, no unlabeled axes?
- Do the titles alone tell the story in order?

Fix what you find and re-render before presenting the deck as done.
