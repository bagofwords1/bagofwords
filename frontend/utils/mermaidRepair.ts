// Best-effort repair for the single most common LLM-authored Mermaid mistake:
// unquoted punctuation inside flowchart NODE labels. Mermaid treats `(`, `)`
// and friends as grammar (they declare node shapes), so a label like
// `E[revenue SUM(Invoice.Total)]` aborts the whole parse and the diagram fails
// to render. Wrapping the label in quotes — `E["revenue SUM(Invoice.Total)"]`
// — is the canonical fix and is what a human would do by hand.
//
// This runs ONLY after Mermaid has already failed to render the raw source
// (see DocMermaid.vue), so it is a rescue pass, not a rewrite of valid input:
// the worst case is that the repaired source also fails and we fall back to
// showing the source exactly as before. Quoting is idempotent and harmless for
// labels that were already valid, so re-quoting an already-quoted label is a
// no-op.
//
// Scope, deliberately narrow:
//   - only `flowchart` / `graph` diagrams (in sequence/class/etc. `[]` and `()`
//     mean other things — we must not touch them)
//   - only NODE labels (edge labels like `-.text.-> ` tolerate punctuation and
//     are left untouched)
//   - shapes are matched longest-delimiter-first so nesting such as `([...])`
//     (stadium) and `[(...)]` (cylinder) keeps its shape

// [openDelimiter, closeDelimiter], multi-char shapes BEFORE single-char so the
// scanner claims `([` before `[`, `[(` before `[`, etc.
const SHAPES: ReadonlyArray<readonly [string, string]> = [
  ['([', '])'], // stadium
  ['[[', ']]'], // subroutine
  ['[(', ')]'], // cylinder / database
  ['((', '))'], // circle
  ['{{', '}}'], // hexagon
  ['[/', '/]'], // parallelogram
  ['[\\', '\\]'], // parallelogram (alt)
  ['[', ']'], // rectangle
  ['(', ')'], // rounded
  ['{', '}'], // rhombus / decision
  ['>', ']'], // asymmetric / flag
]

// A node label opener is only a shape if it immediately follows a node id
// character. This keeps us off `-->` arrowheads, `|edge labels|`, `:::class`
// assignments and any punctuation that lives outside a node declaration.
const ID_CHAR = /[A-Za-z0-9_]/

// What may legally follow a node's closing delimiter: end of statement, a link
// (`-->`, `==>`, `-.->`, `~~~`, `<-->`), node chaining (`&`), a `:::class`
// assignment, or an edge-label pipe. Used to pick the REAL close when the label
// itself contains the close characters, e.g. `((count(n)))` — the outer `))` is
// the one at a statement boundary, not the first `))` we stumble on.
const BOUNDARY_AFTER = /^\s*($|[\n;&|:\-=~<])/

// Index of the close delimiter that sits at a statement boundary (preferred),
// else the last occurrence, else -1.
function findClose(src: string, close: string, start: number): number {
  let fallback = -1
  let at = src.indexOf(close, start)
  while (at !== -1) {
    fallback = at
    if (BOUNDARY_AFTER.test(src.slice(at + close.length))) return at
    at = src.indexOf(close, at + 1)
  }
  return fallback
}

function isFlowchart(src: string): boolean {
  // Skip a leading %%{init}%% directive and/or --- frontmatter --- block, then
  // require the diagram to declare itself as a flowchart/graph.
  const head = src
    .replace(/^\s*%%\{[\s\S]*?\}%%\s*/, '')
    .replace(/^\s*---[\s\S]*?---\s*/, '')
  return /^\s*(flowchart|graph)\b/i.test(head)
}

function quoteLabel(inner: string): string {
  const trimmed = inner.trim()
  // Empty label — leave it alone (quoting `""` is pointless and can confuse).
  if (!trimmed) return inner
  // Already a quoted string — idempotent no-op.
  if (trimmed.startsWith('"') && trimmed.endsWith('"') && trimmed.length >= 2) return inner
  // Escape any raw double-quotes so they survive inside the wrapper.
  const escaped = inner.replace(/"/g, '#quot;')
  return `"${escaped}"`
}

/**
 * Quote unquoted flowchart node labels so punctuation inside them (parentheses,
 * etc.) no longer breaks the Mermaid parser. Returns the source unchanged for
 * non-flowchart diagrams or when there is nothing to quote.
 */
export function repairMermaid(src: string): string {
  if (!src || !isFlowchart(src)) return src

  let out = ''
  let i = 0
  const n = src.length

  while (i < n) {
    const prev = out.length ? out[out.length - 1] : ''
    let matched = false

    if (ID_CHAR.test(prev)) {
      for (const [open, close] of SHAPES) {
        if (!src.startsWith(open, i)) continue
        const start = i + open.length
        const closeIdx = findClose(src, close, start)
        if (closeIdx === -1) continue // no matching close — not a real shape here
        const inner = src.slice(start, closeIdx)
        // A label spanning a newline is almost certainly a mis-parse of two
        // separate statements — leave those for the fallback rather than
        // swallowing a line break into a quoted string.
        if (inner.includes('\n')) continue
        out += open + quoteLabel(inner) + close
        i = closeIdx + close.length
        matched = true
        break
      }
    }

    if (!matched) {
      out += src[i]
      i++
    }
  }

  return out
}
