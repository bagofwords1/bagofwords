// Shared parser for an agent's custom icon override.
//
// The backend stores the override as a namespaced token so the format can grow
// beyond emoji without a schema change:
//   "emoji:<grapheme>"  → render the emoji (e.g. "emoji:📊")
//   "preset:<key>"      → (future) a curated brand/preset icon
//   null / undefined    → no override; fall back to the type/connector icon
//
// Anything this parser doesn't recognise resolves to `kind: 'none'`, so an older
// frontend seeing a newer token (or a garbage value) degrades gracefully to the
// default icon instead of breaking.

export type AgentIconKind = 'emoji' | 'type' | 'preset' | 'none'

export interface ParsedAgentIcon {
  kind: AgentIconKind
  // For 'emoji': the grapheme. For 'type': the connection type/connector key
  // (e.g. "snowflake", "notion"). For 'preset': the preset key. Empty for 'none'.
  value: string
}

export function parseAgentIcon(token: string | null | undefined): ParsedAgentIcon {
  if (!token || typeof token !== 'string') {
    return { kind: 'none', value: '' }
  }
  const raw = token.trim()
  const sep = raw.indexOf(':')
  if (sep === -1) {
    return { kind: 'none', value: '' }
  }
  const kind = raw.slice(0, sep)
  const value = raw.slice(sep + 1).trim()
  if (!value) {
    return { kind: 'none', value: '' }
  }
  if (kind === 'emoji') {
    return { kind: 'emoji', value }
  }
  if (kind === 'type') {
    return { kind: 'type', value }
  }
  // 'preset' is reserved but not yet rendered — fall through to the default icon
  // until the preset gallery ships.
  return { kind: 'none', value: '' }
}

// Build a token to persist from a raw emoji grapheme. Returns null for empty
// input (i.e. "clear the override").
export function emojiToIconToken(emoji: string | null | undefined): string | null {
  const e = (emoji || '').trim()
  return e ? `emoji:${e}` : null
}
