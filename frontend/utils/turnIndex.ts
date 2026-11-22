// Conversation-map merge: the report's turn index (every prompt of the report,
// short server previews, from GET /reports/{id}/turns) overlaid by the turns
// of the pages currently loaded in the transcript.
//
// - Order and membership come from the index (the whole report).
// - A loaded turn wins on text: its prompt is the full text and its answer may
//   still be streaming. An empty loaded answer keeps the index preview.
// - A loaded turn the index does not know yet (sent after the index was
//   fetched) goes right after the loaded turn that precedes it, so a new
//   prompt shows up as a tick immediately.
// - With no index (not fetched yet, or it failed) the loaded turns are used
//   as-is — the map degrades to what it showed before the index existed.

export interface TurnEntry {
	id: string
	prompt: string
	answer: string
}

export function mergeNavTurns<T extends TurnEntry>(index: T[], loaded: T[]): T[] {
	if (!index.length) return loaded
	const loadedById = new Map(loaded.map(t => [t.id, t]))
	const indexPos = new Map(index.map((t, i) => [t.id, i]))
	const after = new Map<number, T[]>() // index position → new turns right after it (-1 = before all)
	let anchor = -1
	for (const t of loaded) {
		const p = indexPos.get(t.id)
		if (p !== undefined) { anchor = p; continue }
		const list = after.get(anchor)
		if (list) list.push(t)
		else after.set(anchor, [t])
	}
	const out: T[] = [...(after.get(-1) || [])]
	index.forEach((t, i) => {
		const l = loadedById.get(t.id)
		out.push(l ? { ...t, prompt: l.prompt || t.prompt, answer: l.answer || t.answer } : t)
		out.push(...(after.get(i) || []))
	})
	return out
}
