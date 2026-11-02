// Text-direction helpers shared by chat markdown (useMarkdownAutoDir), user
// prompt bubbles (InstructionText / reports page) and the instruction
// editor/viewer.
//
// Two strategies live here:
//   - firstStrongDir: what the browser's `dir="auto"` does (first strong
//     character wins). Used where the text is short and typed by the user.
//   - word voting (countDirWords / decideDir): count words by their first
//     strong character and let the majority win. Hebrew/Arabic analysis
//     answers routinely open with a Latin token — a metric name, a table, a
//     bolded English term — which flips a first-strong resolver to LTR even
//     when the rest of the sentence is RTL prose.

export type TextDir = 'rtl' | 'ltr'

const RTL_CHAR = /[֐-ࣿיִ-﷿ﹰ-ﻼ]/
const LTR_CHAR = /[A-Za-zÀ-ɏ]/
// A word that opens with a digit, sign or currency symbol is a quantity
// ("1.2M", "$118", "-5%", "2024", "(5%)"), not prose. Its unit letters must
// not vote, or a Hebrew list of amounts reads as English. Opening brackets and
// quotes are skipped first: "(אלטרנטיבי)" is a Hebrew word, "(5%)" a quantity.
const QUANTITY_START = /^[\p{N}\p{Sc}+\-−±~≈<>]/u
const LEADING_OPENERS = /^[(\[{"'«“„‘‚]+/u

// RTL wins below 50%: RTL prose embeds far more Latin identifiers, acronyms
// and URLs than the reverse, so mostly-Latin text with a stray RTL word stays
// LTR while Hebrew text with several English terms stays RTL.
export const RTL_WORD_THRESHOLD = 0.4
// A list or table aggregates many short items (names, units,
// statuses) whose script says little about the block, so it keeps the
// answer's direction and only leaves it when its text is almost entirely the
// other script: less than 15% RTL words in an RTL answer, more than 85% in an
// LTR one.
export const CONTAINER_MINORITY_SHARE = 0.15
// Blocks this short carry too little text to vote ("Summary", "Active",
// "**MRR**" mid-stream): they follow the surrounding direction.
export const SHORT_BLOCK_WORDS = 3
// A list or table needs more text before it may leave the surrounding
// direction — a table's English header row, or a list's first two Latin
// items, arrive before the Hebrew rows while streaming.
export const SHORT_CONTAINER_WORDS = 8

export function firstStrongDir(text: string): TextDir | null {
  if (!text) return null
  for (const ch of text) {
    if (RTL_CHAR.test(ch)) return 'rtl'
    if (LTR_CHAR.test(ch)) return 'ltr'
  }
  return null
}

export function wordDir(word: string): TextDir | null {
  const core = (word || '').replace(LEADING_OPENERS, '')
  if (!core || QUANTITY_START.test(core)) return null
  return firstStrongDir(core)
}

export interface DirCounts { rtl: number; ltr: number }

export function countDirWords(text: string, into: DirCounts = { rtl: 0, ltr: 0 }): DirCounts {
  if (!text) return into
  for (const word of text.split(/\s+/)) {
    const d = wordDir(word)
    if (d === 'rtl') into.rtl++
    else if (d === 'ltr') into.ltr++
  }
  return into
}

export interface DecideOptions {
  /** Direction of the surrounding content, if any. */
  context?: TextDir | null
  /** List / table: keeps the context unless almost all the other script. */
  container?: boolean
  /** Word count at or below which the block follows the context. */
  minWords?: number
}

// The direction a block should take given its word counts. Returns null only
// when there is neither text to vote nor a context to inherit.
export function decideDir(counts: DirCounts, opts: DecideOptions = {}): TextDir | null {
  const { context = null, container = false } = opts
  const minWords = opts.minWords ?? (container ? SHORT_CONTAINER_WORDS : SHORT_BLOCK_WORDS)
  const total = counts.rtl + counts.ltr
  if (total === 0) return context
  if (context && total <= minWords) return context
  const share = counts.rtl / total
  if (container && context === 'rtl') return share >= CONTAINER_MINORITY_SHARE ? 'rtl' : 'ltr'
  if (container && context === 'ltr') return share > 1 - CONTAINER_MINORITY_SHARE ? 'rtl' : 'ltr'
  return share >= RTL_WORD_THRESHOLD ? 'rtl' : 'ltr'
}

// A value that is only a signed number ("+12%", "-2.1%", "−5", "+1.2M").
// In an RTL paragraph the bidi algorithm resolves the sign by its RTL
// surroundings and draws it after the digits ("12%+"), so these must be laid
// out LTR even inside RTL tables.
const SIGNED_NUMBER = /^[+\-\u2212\u00B1]\s?\p{Sc}?\d[\d.,]*\s?[%\u2030A-Za-z]{0,3}$/u

export function isSignedNumber(text: string | null | undefined): boolean {
  return SIGNED_NUMBER.test((text || '').trim())
}

/** Dominant direction of a plain string (no context). */
export function dominantTextDir(text: string | null | undefined): TextDir | null {
  return decideDir(countDirWords(text || ''))
}

// ─── Direction for a whole rendered document ─────────────────────────────────
// One algorithm for every surface that renders blocks of user/model text: the
// chat markdown observer (DOM), the instruction read view (markdown-it tokens)
// and the instruction editor (ProseMirror nodes). Each builds this small tree
// of its blocks and applies the result its own way, so the three can never
// disagree about which way a block reads.

export type DirNodeKind = 'list' | 'item' | 'table' | 'row' | 'cell' | 'quote' | 'block'

export interface DirNode<R = unknown> {
  kind: DirNodeKind
  /** Word counts of the node's prose, code excluded, nested blocks included. */
  counts: DirCounts
  /** Plain text, needed for cells only (signed-number check). */
  text?: string
  children: DirNode<R>[]
  /** The caller's handle (an element, a token, a node position). */
  ref: R
  /** Set by resolveDirTree. */
  dir?: TextDir | null
  /** Set by resolveDirTree: a cell holding only "+12%" laid out LTR in an RTL table. */
  signedNumber?: boolean
}

// Below this many voting words a document has no reliable direction yet.
export const ROOT_MIN_WORDS = 4

export interface ResolveOptions {
  /** A strong prior, e.g. the direction of the question a reply answers. The
   *  document keeps it unless its text is almost entirely the other script. */
  hint?: TextDir | null
  /** A weak prior (the UI direction), used only while the text is too short. */
  fallback?: TextDir | null
  /** The content is complete (an instruction, a note, a finished render), not
   *  a reply still streaming in. Lists and tables then decide from 4 words
   *  instead of 8: the higher bar only exists so a table's header row, which
   *  arrives before its rows, doesn't flip the table mid-stream. */
  settled?: boolean
}

/**
 * Decide a direction for every node, top-down:
 *  - the document votes once (with the hint as a sticky prior);
 *  - paragraphs, headings and quotes vote on their own words, and blocks of
 *    three words or fewer follow the surrounding direction ("Summary");
 *  - a list or table keeps the surrounding direction unless almost entirely
 *    the other script;
 *  - an item follows its list unless it is more than three words and almost
 *    entirely the other script; an item's own paragraph follows the item;
 *  - rows and cells follow their table; a signed number in an RTL table is
 *    laid out LTR (flagged so the caller keeps it right-aligned).
 * Returns the document direction.
 */
export function resolveDirTree<R>(roots: DirNode<R>[], docCounts: DirCounts, opts: ResolveOptions = {}): TextDir | null {
  const total = docCounts.rtl + docCounts.ltr
  const docDir = opts.hint
    ? decideDir(docCounts, { context: opts.hint, container: true })
    : total < ROOT_MIN_WORDS
      ? (opts.fallback ?? decideDir(docCounts))
      : decideDir(docCounts)

  const visit = (node: DirNode<R>, parent: DirNode<R> | null, context: TextDir | null, tableDir: TextDir | null) => {
    let dir: TextDir | null
    node.signedNumber = false
    switch (node.kind) {
      case 'item': {
        // An item votes on its own text, not its nested lists' ("Always use
        // the fiscal calendar" above a Hebrew sub-list is still English).
        const own = { ...node.counts }
        for (const child of node.children) {
          if (child.kind !== 'list') continue
          own.rtl -= child.counts.rtl
          own.ltr -= child.counts.ltr
        }
        dir = decideDir(own, { context, container: true, minWords: SHORT_BLOCK_WORDS })
        break
      }
      case 'row':
        dir = tableDir ?? context
        break
      case 'cell':
        dir = tableDir ?? context
        if (dir === 'rtl' && isSignedNumber(node.text)) {
          dir = 'ltr'
          node.signedNumber = true
        }
        break
      case 'list':
      case 'table':
        dir = decideDir(node.counts, { context, container: true, minWords: opts.settled ? SHORT_BLOCK_WORDS : undefined })
        break
      default: // block, quote
        dir = parent?.kind === 'item' && node.kind === 'block'
          ? context
          : decideDir(node.counts, { context })
    }
    node.dir = dir
    const nextTable = node.kind === 'table' ? dir : tableDir
    for (const child of node.children) visit(child, node, dir ?? context, nextTable)
  }
  for (const root of roots) visit(root, null, docDir, null)
  return docDir
}

// Locales whose UI direction is RTL — used as the direction fallback for empty
// inputs, where there is no content to derive a direction from (same set as
// PromptBoxV2's MentionInput).
export const RTL_LOCALES = new Set(['he', 'ar', 'fa', 'ur'])
