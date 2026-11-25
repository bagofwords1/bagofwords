// Pure helpers for the ⌘K "In this report" group (CommandPalette.vue).

// "report: churn" → scoped search for "churn". Prefixes are matched
// case-insensitively; the English one always works, a locale may add its own.
export function parseReportScope(query: string, prefixes: string[]): { scoped: boolean; term: string } {
  const raw = query ?? ''
  const trimmed = raw.trimStart()
  const lower = trimmed.toLowerCase()
  for (const p of prefixes) {
    const prefix = (p || '').trim().toLowerCase()
    if (prefix && lower.startsWith(prefix)) {
      return { scoped: true, term: trimmed.slice(prefix.length).trim() }
    }
  }
  return { scoped: false, term: raw.trim() }
}

export interface SnippetPart { text: string; hit: boolean }

// Window a search snippet around the match so it survives one-line truncation,
// and split it so the matched term can be emphasized. Joined back, the parts
// are the (possibly front-trimmed) snippet.
export function snippetParts(snippet: string, term: string, lead = 30): SnippetPart[] {
  let text = snippet || ''
  const needle = (term || '').toLowerCase()
  let at = needle ? text.toLowerCase().indexOf(needle) : -1
  if (at > lead) {
    const cut = text.lastIndexOf(' ', at - Math.floor(lead * 2 / 3))
    const from = cut > 0 ? cut + 1 : at - Math.floor(lead * 2 / 3)
    text = '…' + text.slice(from)
    at = at - from + 1
  }
  if (at < 0) return [{ text, hit: false }]
  return [
    { text: text.slice(0, at), hit: false },
    { text: text.slice(at, at + needle.length), hit: true },
    { text: text.slice(at + needle.length), hit: false },
  ].filter(p => p.text)
}
