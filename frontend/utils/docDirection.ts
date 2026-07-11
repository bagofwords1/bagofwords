// Direction detection for doc artifacts. A document's language comes from the
// content (the planner writes in the user's language), which may differ from
// the UI locale — so we infer direction from the markdown itself rather than
// from the app locale.

const RTL_RE = /[֐-׿؀-ۿ܀-ݏݐ-ݿࢠ-ࣿיִ-﷿ﹰ-﻿]/g
const LTR_RE = /[A-Za-zÀ-ɏЀ-ӿ]/g

/**
 * Infer text direction for a whole document from its markdown.
 *
 * Uses a majority vote of strong-directional characters so a Hebrew/Arabic doc
 * that still contains English identifiers, citations or numbers is correctly
 * classified as RTL. Returns 'ltr' for empty/neutral content (safe default,
 * and avoids `dir="auto"` on an empty contenteditable — which mis-places the
 * caret).
 */
export function detectDocDir(text: string | null | undefined): 'rtl' | 'ltr' {
  if (!text) return 'ltr'
  const rtl = (text.match(RTL_RE) || []).length
  if (rtl === 0) return 'ltr'
  const ltr = (text.match(LTR_RE) || []).length
  return rtl >= ltr ? 'rtl' : 'ltr'
}
