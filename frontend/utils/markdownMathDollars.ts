// Single dollar signs are currency/plain text, never inline math delimiters.
// Keep $$ display math, explicit TeX delimiters, code spans and HTML intact.
// Apply after block parsing so fenced code is never rewritten.

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

/** Escape every standalone dollar outside code/HTML. Idempotent. */
export function escapeNonMathDollars(src: string): string {
  if (!src || !src.includes('$')) return src
  const toEscape = singleDollars(src)
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
