// First-strong-character direction detection, shared by the instruction
// editor/viewer — mirroring what `dir="auto"` does in the browser and what
// useMarkdownAutoDir does for chat markdown. Returns null when the text has
// no strong-direction character, so callers can fall back to inheritance.

const RTL_CHAR = /[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFC]/
const LTR_CHAR = /[A-Za-z\u00C0-\u024F]/

export function firstStrongDir(text: string): 'rtl' | 'ltr' | null {
  if (!text) return null
  for (const ch of text) {
    if (RTL_CHAR.test(ch)) return 'rtl'
    if (LTR_CHAR.test(ch)) return 'ltr'
  }
  return null
}

// Locales whose UI direction is RTL — used as the direction fallback for empty
// inputs, where there is no content to derive a direction from (same set as
// PromptBoxV2's MentionInput).
export const RTL_LOCALES = new Set(['he', 'ar', 'fa', 'ur'])
