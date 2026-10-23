export interface DeleteOutcome {
  id?: string
  title?: string
  outcome?: string
}

export interface DeleteNotes {
  // Never published: deleted with the conversation (phase 7).
  deleted: string[]
  // Published, with changes not yet published: the published version stays,
  // the changes go with the conversation.
  losesChanges: string[]
  // Other conversations keep editing it (collab01): nothing happens to it.
  othersEditing: string[]
}

// The delete confirmation's small lines, from delete_preview outcomes.
// Each dashboard once (by id); two that share a name are said with a count.
export function deleteNotes(dashboards: DeleteOutcome[]): DeleteNotes {
  const names = (outcome: string) => {
    const seen = new Set<string>()
    const counts = new Map<string, number>()
    for (const d of dashboards) {
      if (d.outcome !== outcome) continue
      const key = d.id || `${d.title}:${seen.size}`
      if (seen.has(key)) continue
      seen.add(key)
      const name = d.title || 'Untitled'
      counts.set(name, (counts.get(name) || 0) + 1)
    }
    return [...counts].map(([name, n]) => (n > 1 ? `${name} (${n})` : name))
  }
  return { deleted: names('deleted'), losesChanges: names('loses_changes'), othersEditing: names('others_editing') }
}
