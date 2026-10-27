// Keep currency like "$2,329" out of KaTeX.
//
// markstream-vue's math rule treats any `$…$` pair as inline math, so a reply
// such as "revenue of $2,329 across 412 invoices, avg $5.65" rendered the span
// between the two amounts as italic math. We escape every single `$` that is
// not clearly math before the inline parser sees it; markdown-it then renders
// `\$` as a plain `$`.
//
// A single `$…$` pair counts as math only when (Pandoc's rule):
//   - the opening `$` is followed by a non-space character,
//   - the closing `$` is preceded by a non-space character,
//   - the closing `$` is not followed by a digit.
// `$$…$$`, `\(…\)` and `\[…\]` are left alone, as are code spans, autolinks
// and inline HTML tags.

const isSpace = (ch: string | undefined) => ch === undefined || /\s/.test(ch)
const isDigit = (ch: string | undefined) => ch !== undefined && ch >= '0' && ch <= '9'

function isEscaped(src: string, index: number): boolean {
  let backslashes = 0
  for (let i = index - 1; i >= 0 && src[i] === '\\'; i--) backslashes++
  return backslashes % 2 === 1
}

// Ranges ([start, end)) the escaper must not touch: code spans, autolinks,
// inline HTML tags.
function protectedRanges(src: string): Array<[number, number]> {
  const ranges: Array<[number, number]> = []
  let i = 0
  while (i < src.length) {
    const ch = src[i]
    if (ch === '`' && !isEscaped(src, i)) {
      let run = 1
      while (src[i + run] === '`') run++
      const fence = '`'.repeat(run)
      let j = i + run
      let close = -1
      while ((j = src.indexOf(fence, j)) !== -1) {
        if (src[j + run] !== '`' && src[j - 1] !== '`') { close = j; break }
        while (src[j] === '`') j++
      }
      if (close === -1) { i += run; continue }
      ranges.push([i, close + run])
      i = close + run
      continue
    }
    if (ch === '<') {
      const m = /^<(?:[A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*|\/?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?\/?)>/.exec(src.slice(i))
      if (m) { ranges.push([i, i + m[0].length]); i += m[0].length; continue }
    }
    i++
  }
  return ranges
}

// Index of every unescaped, standalone `$` (not part of `$$`) outside the
// protected ranges.
function singleDollars(src: string): number[] {
  const ranges = protectedRanges(src)
  const out: number[] = []
  let r = 0
  for (let i = 0; i < src.length; i++) {
    while (r < ranges.length && ranges[r][1] <= i) r++
    if (r < ranges.length && ranges[r][0] <= i) { i = ranges[r][1] - 1; continue }
    if (src[i] !== '$' || isEscaped(src, i)) continue
    if (src[i + 1] === '$') {
      // Skip the whole run (`$$`, `$$$`…); those are display-math delimiters.
      while (src[i + 1] === '$') i++
      continue
    }
    out.push(i)
  }
  return out
}

/**
 * Backslash-escape every `$` in an inline markdown string that would not open
 * or close inline math under the rule above. Idempotent.
 */
export function escapeNonMathDollars(src: string): string {
  if (!src || !src.includes('$')) return src
  const dollars = singleDollars(src)
  if (!dollars.length) return src

  const toEscape: number[] = []
  let k = 0
  while (k < dollars.length) {
    const open = dollars[k]
    const next = dollars[k + 1]
    const canOpen = !isSpace(src[open + 1])
    const canClose = next !== undefined && !isSpace(src[next - 1]) && !isDigit(src[next + 1])
    if (canOpen && canClose) {
      k += 2 // a math pair — keep both
      continue
    }
    toEscape.push(open)
    k += 1 // the next `$` may still open its own pair
  }
  if (!toEscape.length) return src

  let out = ''
  let last = 0
  for (const idx of toEscape) {
    out += src.slice(last, idx) + '\\'
    last = idx
  }
  return out + src.slice(last)
}

/**
 * markdown-it plugin: run escapeNonMathDollars over every inline token after
 * block parsing, so fenced/indented code and `$$` math blocks are untouched.
 */
export function currencyDollarPlugin(md: any): void {
  md.core.ruler.after('block', 'escape_currency_dollars', (state: any) => {
    for (const token of state.tokens) {
      if (token.type === 'inline' && typeof token.content === 'string') {
        token.content = escapeNonMathDollars(token.content)
      }
    }
  })
}
