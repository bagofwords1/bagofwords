---
key: evidence-research
title: Evidence research and document analysis
description: Use when the answer has to come out of documents or files — read the source, verify every claim against it, and cite where each one came from.
category: general
version: "1.0"
modes: [chat]
tags: [documents, research]
---

Research from documents fails in a specific way: a plausible summary assembled
from half-read sources, where nobody can tell which sentence came from where.
The discipline is not reading more — it is refusing to assert anything you have
not just verified in the source.

## Work with a note open

`create_note` at the start and `edit_note` as you go. Keep in it: the question
in your own words, a `- [ ]` list of sources to check, findings with their
source and location, contradictions between sources, and hypotheses you have
ruled out. You will read more than you can hold, and the note is what stops the
last document read from dominating the conclusion.

## 1. Establish the corpus before reading

`list_files` / `search_files` / `grep_files` to find what exists; `read_file`
to open it. Before extracting anything, record for each source: what it is, its
**as-of date**, who produced it, and whether it supersedes another. A superseded
document quoted as current is the most damaging thing this workflow produces.

Note the format honestly, because it bounds your confidence:

- **Text-native documents** — extraction is reliable.
- **Scanned images** — the text layer is OCR or absent, and numbers are the
  most error-prone thing OCR produces. Every figure is provisional; say so.
- **Spreadsheets** — `read_excel_as_csv` for a whole sheet, `read_excel_range`
  for a region. Watch for hidden rows, filtered views and multiple sheets that
  disagree.

Generated code **cannot** open `.pdf`, `.docx`, `.pptx` or images — the sandbox
cannot read them at all. Extract with `read_file` first, then hand the values to
`create_data` if you need to compute over them. Never write code that tries to
open those files.

## 2. Verify before you assert — every time

The rule that makes this skill worth enabling: **before any claim reaches the
answer, go back to the source and confirm the exact wording supports it.** Not
your memory of it, not a summary of it. Specifically:

- **Quote or locate.** Every material claim carries a page, section or cell.
- **Check the sentence around it.** Conditions, exclusions and "except where"
  clauses live next to the number, and they change what it means.
- **Distinguish what the document says from what it implies.** Mark inferences
  as inferences.
- **Numbers get a second look**: units and scaling ("$ in thousands" is the
  classic 1000× error), parentheses for negatives, thousands separators,
  footnote markers glued to digits, and whether rows sum to the printed total.
  If a stated total does not reconcile with its own rows, say so and stop —
  do not present a table you know is broken.
- **Tables get a structural look**: multi-line cells and merged headers shift
  values a column sideways, and repeated headers across page breaks read as
  data rows.

## 3. Read across sources, not just within them

Where two documents cover the same thing, compare them explicitly and report the
difference rather than silently preferring one. Where a figure also exists in a
queryable source, reconcile: document value, queried value, difference in
absolute and percent terms. Investigate any gap before explaining it — check
extraction error first (most likely, cheapest to test), then timing (different
as-of dates), scope (an entity one side includes), then definition.

Where the corpus is silent, say it is silent. An absent answer stated plainly is
worth more than an inferred one stated confidently.

## 4. Deliver with the trail intact

`create_doc` for anything with more than a couple of findings. Lead with the
answer, then the evidence, each item citing its source and location. Then two
lists that are the real product of this work:

- **What could not be verified** — figures you could not extract confidently,
  claims resting on a single unconfirmed source, differences you could not
  explain.
- **What the sources disagree on**, with both positions.

State your overall confidence and what would raise it. A short honest answer
beats a complete-looking one with an unverified number inside it.
