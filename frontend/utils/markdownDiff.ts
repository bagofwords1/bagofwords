// Markdown-aware tracked changes for instructions.
//
// A diff used to be printed as plain text spans (`white-space: pre-wrap`,
// `dir="auto"`), so a change to an instruction showed its markdown raw (`**`,
// `#`, table pipes) and every line opening with a Latin word flipped LTR. This
// renders it the way the instruction itself renders (utils/instructionMarkdown):
//
//  1. The diff is laid out on the lines of the NEW text. Insertions and kept
//     text are markdown; deleted text inside a line is replaced by an inert
//     placeholder, so removed `**` or `|` can never change the structure of
//     what remains. A line deleted as a whole keeps its markdown (a removed
//     list item still reads as a list item, struck through).
//  2. Insert/delete runs are delimited by Private Use Area sentinels instead of
//     HTML tags: markdown-it treats them as ordinary text, so they survive
//     inside headings, cells and fenced code alike. Block syntax a line opens
//     with (`# `, `- `, `1. `, `> `) and table pipes are kept OUTSIDE the
//     sentinels so they still parse as structure.
//  3. After rendering, each text node is split at the sentinels and the marked
//     runs are wrapped in <ins>/<del> one text node at a time
//     (applyDiffMarks in utils/markdownDiffRender.ts), so the tags are always
//     well nested no matter how the run crosses bold, links or cells.
//  4. Deleted text never votes on direction (diffVoteText).
//
// This module is DOM-free so it can be unit-tested in Node.
import DiffMatchPatch from 'diff-match-patch'

export type DiffMark = 'ins' | 'del'

export type DiffPiece =
  | { kind: 'equal'; text: string }
  | { kind: 'insert' | 'delete'; text: string; hunk?: number }
  | { kind: 'gap'; lines: number }

export interface InlineMark { text: string; mark: DiffMark; hunk?: number }

export interface MarkdownDiffSource {
  /** Markdown with sentinels, ready for createInstructionMarkdown(...).render. */
  source: string
  /** Inline placeholders: deleted text, or a ¶ standing in for a change with no visible text. */
  inlineMarks: InlineMark[]
  /** Collapsed unchanged regions, by placeholder index: the number of hidden lines. */
  gaps: number[]
  /** Hunks that ended up with no visible marked text at all. */
  invisibleHunks: Set<number>
  /** Every hunk that appears in the diff. */
  hunks: Set<number>
}

export interface BuildOptions {
  /** Turns @mentions in a run of markdown into chip HTML (never applied inside code). */
  mentionize?: (text: string) => string
  /** Hunks that need a visible ¶ marker because they have no visible text of their own. */
  forceVisible?: Set<number>
}

// ─── Sentinels ───────────────────────────────────────────────────────────────
const OPEN = ''
const OPEN_END = ''
const CLOSE = ''
const PH = ''
const PH_END = ''
const GAP = ''
const GAP_END = ''
const MARK_CHAR: Record<DiffMark, string> = { ins: '', del: '' }
const DIGIT0 = 0xE020

const SENTINEL_CHARS = /[-]/g
export const HAS_SENTINEL = /[-]/

const num = (n: number) => String(n).split('').map(d => String.fromCharCode(DIGIT0 + Number(d))).join('')
const unnum = (s: string) => Number(Array.from(s).map(c => c.charCodeAt(0) - DIGIT0).join(''))

const openMark = (mark: DiffMark, hunk?: number) => OPEN + MARK_CHAR[mark] + (hunk == null ? '' : num(hunk)) + OPEN_END
const placeholder = (i: number) => PH + num(i) + PH_END
const gapToken = (i: number) => GAP + num(i) + GAP_END

export type SentinelToken =
  | { type: 'text'; value: string }
  | { type: 'open'; mark: DiffMark; hunk?: number }
  | { type: 'close' }
  | { type: 'inline'; index: number }
  | { type: 'gap'; index: number }

const TOKEN_RE = /([])([-]*)||([-]+)|([-]+)/g

/** Split rendered text into plain runs and sentinel tokens. Stray sentinels are dropped. */
export function parseSentinels(text: string): SentinelToken[] {
  const out: SentinelToken[] = []
  const pushText = (value: string) => {
    const clean = value.replace(SENTINEL_CHARS, '')
    if (clean) out.push({ type: 'text', value: clean })
  }
  let last = 0
  TOKEN_RE.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = TOKEN_RE.exec(text))) {
    if (m.index > last) pushText(text.slice(last, m.index))
    if (m[1]) out.push({ type: 'open', mark: m[1] === MARK_CHAR.ins ? 'ins' : 'del', hunk: m[2] ? unnum(m[2]) : undefined })
    else if (m[3]) out.push({ type: 'inline', index: unnum(m[3]) })
    else if (m[4]) out.push({ type: 'gap', index: unnum(m[4]) })
    else out.push({ type: 'close' })
    last = TOKEN_RE.lastIndex
  }
  if (last < text.length) pushText(text.slice(last))
  return out
}

/** Remove every sentinel character (for attributes such as href). */
export function stripSentinels(text: string): string {
  return text.replace(SENTINEL_CHARS, '')
}

/**
 * Direction vote for one text token of a rendered diff: deleted text is
 * excluded, so a struck-out English word at the start of a Hebrew line no
 * longer decides the line. State (inside a deletion or not) is carried on
 * `env`, because a deleted run can span several tokens (bold, code spans).
 */
export function diffVoteText(content: string, env: any): { vote: string; text: string } {
  const state = env || {}
  if (!HAS_SENTINEL.test(content)) {
    return state.__diffInDel ? { vote: '', text: '' } : { vote: content, text: content }
  }
  let kept = ''
  for (const tok of parseSentinels(content)) {
    if (tok.type === 'open') state.__diffInDel = tok.mark === 'del'
    else if (tok.type === 'close') state.__diffInDel = false
    else if (tok.type === 'text' && !state.__diffInDel) kept += tok.value
  }
  return { vote: kept, text: kept }
}

// ─── Building the source ─────────────────────────────────────────────────────

type PartKind = 'eq' | 'ins' | 'del' | 'inline'
interface Part { kind: PartKind; text: string; hunk?: number; mark?: DiffMark }
type Line = { parts: Part[]; wholeDel: boolean } | { gap: number }

const FENCE_RE = /^\s{0,3}(`{3,}|~{3,})/
// Lines that are pure structure: a table's delimiter row, a thematic break, a
// setext underline. Markers inside them would stop them parsing.
const TABLE_DELIM_RE = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/
const THEMATIC_RE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/
const SETEXT_EQ_RE = /^\s{0,3}=+\s*$/
// Block syntax a line opens with: indentation, quote markers, list markers
// (nested), then an ATX heading marker.
const PREFIX_RE = /^[ \t]*(?:(?:>[ \t]?)|(?:[-*+][ \t]+)|(?:\d{1,9}[.)][ \t]+))*(?:#{1,6}(?:[ \t]+|$))?/

const isVisible = (p: Part, wholeDel: boolean) => p.kind === 'eq' || p.kind === 'ins' || (wholeDel && p.kind === 'del')

/** Split diff pieces into lines of the new text (plus whole deleted lines). */
function toLines(pieces: DiffPiece[], forceVisible: Set<number>): Line[] {
  const lines: Line[] = []
  let cur: Part[] = []
  const endLine = () => { lines.push({ parts: cur, wholeDel: false }); cur = [] }
  const pilcrowed = new Set<number>()

  for (const p of pieces) {
    if (p.kind === 'gap') {
      if (cur.length) endLine()
      lines.push({ gap: p.lines })
      continue
    }
    if (!p.text) continue
    const hunk = p.kind === 'equal' ? undefined : p.hunk
    // A change that would otherwise have nothing visible to point at (a
    // paragraph split, a list marker, a link target) gets a ¶ at the start of
    // its line — never mid-line, where it could land inside a link target.
    // It is an inert inline placeholder, so it never alters the structure.
    if (hunk != null && forceVisible.has(hunk) && !pilcrowed.has(hunk)) {
      pilcrowed.add(hunk)
      cur.unshift({ kind: 'inline', text: '¶', hunk, mark: p.kind === 'delete' ? 'del' : 'ins' })
    }
    if (p.kind === 'equal' || p.kind === 'insert') {
      const kind: PartKind = p.kind === 'equal' ? 'eq' : 'ins'
      p.text.split('\n').forEach((chunk, i) => {
        if (i > 0) endLine()
        if (chunk) cur.push({ kind, text: chunk, hunk })
      })
      continue
    }
    // Deletion. Whole lines removed at a line start are kept as lines (with
    // their markdown) so a removed item or row still reads as one. Anything
    // else is an inline placeholder: it does not take part in parsing.
    const chunks = p.text.split('\n')
    // Only a ¶ marker may precede it for the deletion to start the line.
    const atLineStart = cur.every(c => c.kind === 'inline' && c.text === '¶')
    if (chunks.length === 1 || !atLineStart) {
      cur.push({ kind: 'inline', text: p.text, hunk, mark: 'del' })
      continue
    }
    const whole = chunks.slice(0, -1)
    // A removed blank line on its own would split paragraphs the new text
    // joins, so it is dropped. Removed together with removed content (a
    // deleted table or list and the blank line after it) it is kept: without
    // it the next kept paragraph would read as part of the deleted block.
    const keepBlanks = whole.some(c => c.trim())
    for (const chunk of whole) {
      if (chunk.trim()) lines.push({ parts: [{ kind: 'del', text: chunk, hunk }], wholeDel: true })
      else if (keepBlanks) lines.push({ parts: [], wholeDel: true })
    }
    const last = chunks[chunks.length - 1]
    if (last) cur.push({ kind: 'inline', text: last, hunk, mark: 'del' })
  }
  if (cur.length) endLine()
  return lines
}

const isStructural = (visible: string) =>
  (TABLE_DELIM_RE.test(visible) && visible.includes('-')) || THEMATIC_RE.test(visible) || SETEXT_EQ_RE.test(visible)

/** The changed parts of a line emitted as plain structure, as inert placeholders. */
const changesOf = (parts: Part[]): Part[] => parts
  .filter(p => p.kind !== 'eq')
  .map((p): Part => (p.kind === 'inline' ? p : { kind: 'inline', text: p.text, hunk: p.hunk, mark: p.kind === 'ins' ? 'ins' : 'del' }))

export function buildMarkdownDiffSource(pieces: DiffPiece[], opts: BuildOptions = {}): MarkdownDiffSource {
  const forceVisible = opts.forceVisible || new Set<number>()
  const mentionize = opts.mentionize || ((s: string) => s)
  const inlineMarks: InlineMark[] = []
  const gaps: number[] = []
  const seenHunks = new Set<number>()
  const visibleHunks = new Set<number>()
  const out: string[] = []
  let fence: { char: string; len: number } | null = null
  // Changes on a line that must stay plain structure (a fence, a table's
  // delimiter row, a thematic break) are shown as placeholders at the start
  // of the next line with content, so they can still be seen and reviewed.
  let carried: Part[] = []
  let lastContentLine = -1
  let prevWholeDelRow = false

  const noteHunk = (hunk: number | undefined, text: string) => {
    if (hunk == null) return
    seenHunks.add(hunk)
    if (text.trim()) visibleHunks.add(hunk)
  }

  const emitInline = (p: Part) => {
    noteHunk(p.hunk, p.text)
    inlineMarks.push({ text: p.text, mark: p.mark || 'del', hunk: p.hunk })
    return placeholder(inlineMarks.length - 1)
  }

  // Wrap a run in sentinels, leaving table pipes outside so cells still split.
  const wrap = (text: string, mark: DiffMark, hunk: number | undefined, inCode: boolean) => {
    noteHunk(hunk, text)
    const chunks = inCode ? [text] : text.split(/((?<!\\)\|)/)
    return chunks.map((chunk) => {
      if (!chunk) return ''
      if (!inCode && chunk === '|') return chunk
      const body = inCode ? chunk : mentionize(chunk)
      return openMark(mark, hunk) + body + CLOSE
    }).join('')
  }

  const emitPart = (p: Part, wholeDel: boolean, inCode: boolean) => {
    if (p.kind === 'inline' || (p.kind === 'del' && !wholeDel)) return emitInline(p)
    if (p.kind === 'eq') return inCode ? p.text : mentionize(p.text)
    return wrap(p.text, p.kind === 'ins' ? 'ins' : 'del', p.hunk, inCode)
  }

  const pushPlain = (visible: string, parts: Part[]) => {
    carried.push(...changesOf(parts))
    out.push(visible)
  }
  const pushContent = (text: string) => {
    lastContentLine = out.length
    out.push(text)
  }

  for (const line of toLines(pieces, forceVisible)) {
    if ('gap' in line) {
      gaps.push(line.gap)
      // Blank lines around keep the gap its own block.
      out.push('', gapToken(gaps.length - 1), '')
      continue
    }
    const { wholeDel } = line
    let parts = line.parts
    for (const p of parts) if (p.hunk != null) seenHunks.add(p.hunk)
    const visible = parts.filter(p => isVisible(p, wholeDel)).map(p => p.text).join('')
    const fenceMatch = visible.match(FENCE_RE)

    // A removed fence or structure line is not re-parsed: as raw markdown it
    // would open a code block (or turn kept text into a heading) that the new
    // text does not have. Its text is shown as a deletion on the next line.
    // Exception: a removed table's delimiter row right under its removed
    // header row only shapes that removed table, so it stays structure and
    // the whole removed table reads as a (struck-through) table.
    const removedTableDelim = wholeDel && prevWholeDelRow && TABLE_DELIM_RE.test(visible) && visible.includes('-')
    if (removedTableDelim) { out.push(visible); prevWholeDelRow = false; continue }
    if (wholeDel && (fenceMatch || isStructural(visible))) {
      carried.push(...changesOf(parts))
      prevWholeDelRow = false
      continue
    }
    prevWholeDelRow = wholeDel && !fence && /^\s*\|/.test(visible)

    if (fence) {
      const closes = fenceMatch && fenceMatch[1][0] === fence.char && fenceMatch[1].length >= fence.len
        && !visible.slice(fenceMatch[0].length).trim()
      if (closes) { fence = null; pushPlain(visible, parts); continue }
      if (carried.length) { parts = [...carried, ...parts]; carried = [] }
      pushContent(parts.map(p => emitPart(p, wholeDel, true)).join(''))
      continue
    }
    if (fenceMatch) {
      fence = { char: fenceMatch[1][0], len: fenceMatch[1].length }
      pushPlain(visible, parts)
      continue
    }
    if (isStructural(visible)) {
      pushPlain(visible, parts)
      continue
    }
    if (!parts.length) { out.push(''); continue }

    // Hoist the block prefix (and a table row's leading pipe) out of any
    // marked run, so it still parses as structure.
    const blockPrefix = (visible.match(PREFIX_RE) || [''])[0]
    const pipe = visible.slice(blockPrefix.length).match(/^\|[ \t]*/)
    const prefix = blockPrefix + (pipe ? pipe[0] : '')
    let need = prefix.length
    const lead: Part[] = [...carried]
    carried = []
    const body: Part[] = []
    for (const p of parts) {
      if (!isVisible(p, wholeDel)) { (need > 0 ? lead : body).push(p); continue }
      if (need >= p.text.length) {
        need -= p.text.length
        // The change was entirely block syntax (a new list marker, a heading
        // level): it stays visible through its ¶ on the next pass.
        if (p.kind !== 'eq') noteHunk(p.hunk, '')
        continue
      }
      if (need > 0) { body.push({ ...p, text: p.text.slice(need) }); need = 0; continue }
      body.push(p)
    }
    pushContent(prefix + [...lead, ...body].map(p => emitPart(p, wholeDel, false)).join(''))
  }

  // Structure changes at the very end: attach them to the last line with content.
  if (carried.length) {
    const tail = carried.map(p => emitInline(p)).join('')
    if (lastContentLine >= 0 && !fence) out[lastContentLine] += tail
    else out.push('', tail)
  }

  const invisibleHunks = new Set<number>()
  for (const h of seenHunks) if (!visibleHunks.has(h)) invisibleHunks.add(h)
  return { source: out.join('\n'), inlineMarks, gaps, invisibleHunks, hunks: seenHunks }
}

/** Drop sentinel characters the text itself may contain: they are reserved for marking. */
const cleanPieces = (pieces: DiffPiece[]): DiffPiece[] =>
  pieces.map(p => (p.kind === 'gap' ? p : { ...p, text: stripSentinels(p.text || '') }))

/**
 * Build the source, and if some change has nothing visible to hover, build it
 * once more with a ¶ marker for exactly those changes (plus any the caller
 * found missing after rendering, via `forceVisible`).
 */
export function buildMarkdownDiff(input: DiffPiece[], opts: BuildOptions = {}): MarkdownDiffSource {
  const pieces = refinePieces(cleanPieces(input))
  const first = buildMarkdownDiffSource(pieces, opts)
  if (!first.invisibleHunks.size) return first
  const forceVisible = new Set([...(opts.forceVisible || []), ...first.invisibleHunks])
  return buildMarkdownDiffSource(pieces, { ...opts, forceVisible })
}

// ─── Line-aligned refinement ─────────────────────────────────────────────────
// A character diff happily merges two edits on neighbouring lines into one run
// that crosses the newline between them ("…מכירות[-\n\nKPI ראשי הוא][+ ורווחיות\n\nRevenue…]"),
// which would put the removed start of a paragraph inside the heading above it.
// Each change is therefore widened to whole lines and diffed again: by line
// first, then by character within the lines that changed. A change keeps its
// hunk, so per-change Accept/Reject is unaffected.

const isChange = (p: DiffPiece | undefined): p is Extract<DiffPiece, { kind: 'insert' | 'delete' }> =>
  !!p && (p.kind === 'insert' || p.kind === 'delete')

// Word-level diff inside the changed lines: whole words (and whole @mentions)
// change, rather than a few letters in the middle of one. Tokens are mapped to
// single characters, diffed, then mapped back — the same trick as
// diff-match-patch's line mode.
function diffWords(dmp: any, a: string, b: string): Array<[number, string]> {
  const tokens: string[] = []
  const index = new Map<string, number>()
  const encode = (text: string) => (text.match(/\s+|[^\s]+/g) || []).map((tok) => {
    let i = index.get(tok)
    if (i === undefined) { i = tokens.length; tokens.push(tok); index.set(tok, i) }
    return String.fromCharCode(i + 1)
  }).join('')
  const ea = encode(a)
  const eb = encode(b)
  // Past the BMP-safe range the mapping would collide: fall back to characters.
  if (tokens.length > 60000) { const d = dmp.diff_main(a, b); dmp.diff_cleanupSemantic(d); return d }
  const d: Array<[number, string]> = dmp.diff_main(ea, eb, false)
  dmp.diff_cleanupSemantic(d)
  return d.map(([t, s]) => [t, Array.from(s).map(c => tokens[c.charCodeAt(0) - 1]).join('')])
}

function diffLinesThenChars(before: string, after: string, hunk: number | undefined): DiffPiece[] {
  const dmp: any = new (DiffMatchPatch as any)()
  const a = dmp.diff_linesToChars_(before, after)
  const lineDiffs: Array<[number, string]> = dmp.diff_main(a.chars1, a.chars2, false)
  dmp.diff_charsToLines_(lineDiffs, a.lineArray)
  const out: DiffPiece[] = []
  let del = ''
  let ins = ''
  const flush = () => {
    if (!del && !ins) return
    const d = diffWords(dmp, del, ins)
    for (const [t, s] of d) {
      if (!s) continue
      out.push(t === 0 ? { kind: 'equal', text: s } : { kind: t === 1 ? 'insert' : 'delete', text: s, hunk })
    }
    del = ''
    ins = ''
  }
  for (const [t, s] of lineDiffs) {
    if (t === 0) { flush(); if (s) out.push({ kind: 'equal', text: s }) }
    else if (t === -1) del += s
    else ins += s
  }
  flush()
  return out
}

export function refinePieces(input: DiffPiece[]): DiffPiece[] {
  const pieces: DiffPiece[] = input.map(p => ({ ...p }))
  const out: DiffPiece[] = []
  let i = 0
  while (i < pieces.length) {
    const p = pieces[i]
    if (!isChange(p)) { out.push(p); i++; continue }
    const hunk = p.hunk
    // One change: consecutive inserts/deletes of the same hunk, plus equal
    // text between them that stays on the same line.
    let end = i
    let j = i + 1
    while (j < pieces.length) {
      const q = pieces[j]
      if (isChange(q) && q.hunk === hunk) { end = j; j++; continue }
      if (q.kind === 'equal' && !q.text.includes('\n') && isChange(pieces[j + 1]) && (pieces[j + 1] as any).hunk === hunk) { j++; continue }
      break
    }
    // Widen to whole lines, taking the context from the neighbouring equal text.
    let left = ''
    const prev = out[out.length - 1]
    if (prev && prev.kind === 'equal') {
      const k = prev.text.lastIndexOf('\n')
      left = prev.text.slice(k + 1)
      prev.text = prev.text.slice(0, k + 1)
      if (!prev.text) out.pop()
    }
    let right = ''
    const next = pieces[end + 1]
    if (next && next.kind === 'equal') {
      const k = next.text.indexOf('\n')
      right = k < 0 ? next.text : next.text.slice(0, k)
      next.text = k < 0 ? '' : next.text.slice(k)
    }
    let before = left
    let after = left
    for (let k = i; k <= end; k++) {
      const q = pieces[k]
      if (q.kind === 'gap') continue
      if (q.kind !== 'insert') before += q.text
      if (q.kind !== 'delete') after += q.text
    }
    before += right
    after += right
    out.push(...diffLinesThenChars(before, after, hunk))
    i = end + 1
  }
  return out.filter(p => p.kind === 'gap' || p.text)
}

/** Diff ops as produced by diff-match-patch (-1 delete, 0 equal, 1 insert). */
export function piecesFromDiffOps(ops: Array<{ type: number; text: string }>): DiffPiece[] {
  return (ops || []).map(op =>
    op.type === 0 ? { kind: 'equal', text: op.text }
      : { kind: op.type === 1 ? 'insert' : 'delete', text: op.text },
  )
}
