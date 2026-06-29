// Shared helpers for "consuming" a saved Prompt into the prompt box.
//
// A selected prompt is substituted into plain text (its `{{name}}` placeholders
// replaced with the user-provided parameter values) and the resulting text is
// pasted into the box — the prompt itself is NOT kept as a persistent chip.
// Its `mentions` are merged into whatever the box already has, with the user's
// existing mentions winning on any id collision.

export interface PromptParameter {
  name: string
  label?: string
  type?: 'text' | 'number' | 'enum' | 'date' | 'date_range'
  required?: boolean
  default?: any
  options?: any[]
}

// A `date_range` value is a { start, end } pair; everything else is a scalar.
export type PromptParamValue = string | number | { start?: string; end?: string } | undefined

// Mentions are PromptSchema-compatible groups: [{ name, items: [{ id, ... }] }].
export interface MentionGroup {
  name: string
  items: any[]
}

export function usePromptFill() {
  // Extract the unique `{{name}}` placeholder names from a prompt's text, in
  // first-seen order. This is the single source of truth for which parameters a
  // prompt has — there is no separate parameter schema; the text drives it.
  function extractParamNames(text: string): string[] {
    if (!text) return []
    const names: string[] = []
    const seen = new Set<string>()
    const re = /\{\{\s*([\w.-]+)\s*\}\}/g
    let m: RegExpExecArray | null
    while ((m = re.exec(text)) !== null) {
      const name = String(m[1]).trim()
      if (name && !seen.has(name)) {
        seen.add(name)
        names.push(name)
      }
    }
    return names
  }

  // Replace every `{{name}}` placeholder in `text` with the matching value.
  // - date_range → "<start> to <end>"
  // - missing values collapse the placeholder to an empty string.
  function substitute(text: string, values: Record<string, PromptParamValue>): string {
    if (!text) return ''
    return text.replace(/\{\{\s*([\w.-]+)\s*\}\}/g, (_match, rawName: string) => {
      const name = String(rawName).trim()
      const v = values?.[name]
      if (v == null) return ''
      if (typeof v === 'object') {
        const start = (v as any).start ?? ''
        const end = (v as any).end ?? ''
        if (!start && !end) return ''
        return `${start} to ${end}`
      }
      return String(v)
    })
  }

  // Merge a prompt's mention groups into the box's current groups.
  // Dedup is by item id within each named group; the current (user's) mentions
  // win, so a prompt can never override what the user already selected.
  function mergeMentions(current: MentionGroup[], promptMentions: MentionGroup[]): MentionGroup[] {
    const result: MentionGroup[] = (current || []).map(g => ({ name: g.name, items: [...(g.items || [])] }))
    const byName = new Map<string, MentionGroup>()
    for (const g of result) byName.set(g.name, g)

    for (const pg of promptMentions || []) {
      if (!pg || !Array.isArray(pg.items)) continue
      let group = byName.get(pg.name)
      if (!group) {
        group = { name: pg.name, items: [] }
        byName.set(pg.name, group)
        result.push(group)
      }
      const seen = new Set(group.items.map((it: any) => String(it?.id)))
      for (const item of pg.items) {
        const id = String(item?.id)
        if (!id || seen.has(id)) continue // current wins on collision
        seen.add(id)
        group.items.push(item)
      }
    }

    return result
  }

  return { substitute, mergeMentions, extractParamNames }
}
