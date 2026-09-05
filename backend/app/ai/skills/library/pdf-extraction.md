---
key: pdf-extraction
title: Extract and reconcile data from PDF documents
description: Use when a PDF report, statement or filing is the source — extract its figures and reconcile them against the warehouse.
category: general
version: "1.0"
tags: [documents, reconciliation]
---

# Extract and reconcile data from a PDF

PDFs carry numbers that exist nowhere else: a vendor statement, a filing, a
board pack, a lab report. The work is not "read the PDF" — it is getting
numbers out of a layout-oriented format without silently corrupting them, and
then reconciling them against data you can query.

## 1. Read it with the right tool

Use the `read_file` tool. Generated code **cannot** open PDFs — the sandbox
cannot read `.pdf`, `.docx`, `.pptx` or images at all, so never write code that
tries. Extract first with the tool, then hand the extracted values to
`create_data` if you need to compute over them.

Before extracting anything, establish what kind of PDF this is, because it
determines how much you can trust the output:

- **Text-native** (exported from a system) — extraction is reliable.
- **Scanned image** — the text layer may be OCR output or absent. Numbers are
  the most error-prone thing OCR produces. Treat every figure as provisional
  and say so.
- **Mixed** — common in filings; check page by page.

## 2. Extract tables defensively

Table extraction from PDF is where wrong numbers enter unnoticed. Check each
of these before using an extracted table:

- **Do the rows sum to the printed total?** If the document shows a total, verify
  it against the sum of your extracted rows. This single check catches most
  extraction failures. If it does not reconcile, say so and stop — do not
  present a table you know is broken.
- **Column alignment.** Multi-line cells and merged headers routinely shift
  values one column left or right. Spot-check several rows against the
  original text.
- **Number formatting.** Thousands separators, currency symbols, parentheses
  for negatives `(1,234)` = -1234, trailing minus, footnote markers glued to
  digits, and unit scaling in the header ("$ in thousands" is the classic
  1000x error). Normalize deliberately and state the units you settled on.
- **Continued tables** across page breaks — headers repeat, and the repeat is
  easily read as a data row.

## 3. Reconcile against queryable data

Extraction is rarely the goal; agreement is. When the same quantity exists in
a connected source, compare them and report the difference explicitly:

- Match on the document's grain (period, entity, account).
- Show document value, warehouse value, and the difference — absolute and
  percent — as a table.
- For every non-zero difference, investigate before explaining: timing
  (the document may be as-of a different date), scope (a subsidiary or region
  the query includes and the document does not), definition (net vs. gross),
  or an extraction error. Rule out the extraction error first — it is the most
  likely and the cheapest to check.

## 4. Report

State up front what the document is, its as-of date, and how the numbers were
extracted (text layer vs. OCR). Present the reconciliation table. List, plainly,
every figure you could not extract confidently and every difference you could
not explain — a short honest list beats a complete-looking table with a wrong
number in it.

Cite page numbers for anything material, so a reader can check you.
