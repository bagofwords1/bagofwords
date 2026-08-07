import DiffMatchPatch from 'diff-match-patch'

export type HunkOpType = -1 | 0 | 1
export interface HunkOp { type: HunkOpType; text: string }

// A hunk arrives from the server as two whole strings (`before` / `after`).
// For a phrase-sized change that IS the readable form. But the server's
// word-level diff is quadratic, so above MAX_DIFF_TOKENS (backend
// app/services/text_hunks.py) it degrades to a single `oversized` hunk holding
// the ENTIRE instruction on both sides — which renders as the whole document
// struck through followed by the whole document in green, with the handful of
// genuinely changed words invisible somewhere in the middle.
//
// Refining that pair in the browser costs nothing: diff_main strips the common
// prefix/suffix and then runs its line-mode pass, which resolves a 257KB pair
// carrying three scattered edits in ~11ms.
//
// This deliberately diffs RAW CHARACTERS. Encoding word tokens to synthetic
// chars first (as KnowledgeExplorer's `wordDiffOps` does, for whole-word
// replacement granularity) leaves no literal "\n" in the encoded string, so
// line mode finds no lines, falls through to bisect, and trips dmp's 1s
// Diff_Timeout — whose failure mode is exactly the whole-text delete+insert
// this is here to remove.
let _dmp: any = null
function dmp() {
  if (!_dmp) _dmp = new (DiffMatchPatch as any)()
  return _dmp
}

// Under this, the two sides are short enough to read as a plain
// before/after pair, and refining them would only fragment single words
// ("cost center" -> "cost centre" reading as "cent|er|re" rather than as two
// whole words).
const REFINE_MIN_CHARS = 120

export function shouldRefineHunk(hunk: { before?: string; after?: string; oversized?: boolean }): boolean {
  const before = hunk.before || ''
  const after = hunk.after || ''
  // A pure insertion or deletion is already minimal — there is no counterpart
  // to diff it against, and every character of it really did change.
  if (!before || !after) return false
  if (hunk.oversized) return true
  return Math.min(before.length, after.length) >= REFINE_MIN_CHARS
}

const isSpace = (ch: string) => /\s/.test(ch)
/** Length of the partial word `s` ends with (0 if it ends on whitespace). */
function tailWordLen(s: string): number {
  let i = s.length
  while (i > 0 && !isSpace(s[i - 1])) i--
  return s.length - i
}
/** Length of the partial word `s` starts with (0 if it starts on whitespace). */
function headWordLen(s: string): number {
  let i = 0
  while (i < s.length && !isSpace(s[i])) i++
  return i
}

/**
 * Grow every changed run out to whole words. A raw character diff of
 * "cost center" -> "cost centre" lands on the letters that moved, which reads
 * as "cost cent~~e~~re"; what a reviewer wants is "cost ~~center~~centre".
 *
 * Only ever re-partitions the same characters — text is moved between adjacent
 * ops, and any word fragment pulled out of an unchanged run is added to BOTH
 * sides (it is present in `before` and in `after`), so both sides still
 * reconstruct exactly. Runs separated by an unchanged fragment carrying no
 * whitespace are merged, since they are edits inside one word.
 */
function expandToWordBoundaries(ops: HunkOp[]): HunkOp[] {
  const out: HunkOp[] = []
  let del = ''
  let ins = ''
  let open = false
  let carry = ''   // partial word held back from the last unchanged run
  const pushEqual = (text: string) => {
    if (!text) return
    const prev = out[out.length - 1]
    if (prev && prev.type === 0) prev.text += text   // keep unchanged text in one run
    else out.push({ type: 0, text })
  }
  const flush = () => {
    if (del) out.push({ type: -1, text: del })
    if (ins) out.push({ type: 1, text: ins })
    del = ''; ins = ''; open = false
  }
  for (const op of ops) {
    if (op.type !== 0) {
      if (!open) { del = carry; ins = carry; carry = ''; open = true }
      if (op.type === -1) del += op.text
      else ins += op.text
      continue
    }
    let rest: string
    if (open) {
      const head = headWordLen(op.text)
      // The word the open run started in continues past this unchanged text.
      if (head === op.text.length) { del += op.text; ins += op.text; continue }
      del += op.text.slice(0, head)
      ins += op.text.slice(0, head)
      flush()
      rest = op.text.slice(head)
    } else {
      rest = carry + op.text
      carry = ''
    }
    const tail = tailWordLen(rest)
    pushEqual(rest.slice(0, rest.length - tail))
    carry = rest.slice(rest.length - tail)
  }
  flush()
  pushEqual(carry)
  return out
}

/** Ops rendering `before` -> `after`: type 0 unchanged, -1 removed, 1 added. */
export function refineHunkOps(before: string, after: string): HunkOp[] {
  const d = dmp()
  const ops = d.diff_main(before, after)
  // Glue the character runs back into phrases wherever it can; the word-boundary
  // pass then finishes the job on the fragments it leaves inside a word.
  d.diff_cleanupSemantic(ops)
  const raw = (ops as [number, string][])
    .filter(([, text]) => text !== '')
    .map(([type, text]) => ({ type: type as HunkOpType, text }))
  return expandToWordBoundaries(raw)
}

/**
 * The ops a hunk should render as: refined when the raw pair would be
 * unreadable, otherwise the unchanged before/after pair the server sent.
 */
export function hunkOps(hunk: { before?: string; after?: string; oversized?: boolean }): HunkOp[] {
  const before = hunk.before || ''
  const after = hunk.after || ''
  if (shouldRefineHunk(hunk)) return refineHunkOps(before, after)
  const ops: HunkOp[] = []
  if (before) ops.push({ type: -1, text: before })
  if (after) ops.push({ type: 1, text: after })
  return ops
}
