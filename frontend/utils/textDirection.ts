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
// ("1.2M", "$118", "-5%", "2024"), not prose. Its unit letters must not vote,
// or a Hebrew list of amounts reads as English.
const QUANTITY_START = /^[\p{N}\p{Sc}+\-−±~≈<>(]/u

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
  if (!word || QUANTITY_START.test(word)) return null
  return firstStrongDir(word)
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

// Locales whose UI direction is RTL — used as the direction fallback for empty
// inputs, where there is no content to derive a direction from (same set as
// PromptBoxV2's MentionInput).
export const RTL_LOCALES = new Set(['he', 'ar', 'fa', 'ur'])
