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

// Jump-to-message with "the latest click wins".
//
// A jump to a turn that isn't loaded pages older history until it is, then
// scrolls to it. Clicks that arrive while that load is in flight must not be
// lost or overridden:
// - a click on another unloaded turn retargets the running load (the next
//   page request reaches back to the new target, and only it is scrolled to);
// - a click on an already-loaded turn scrolls there at once and cancels the
//   pending scroll, so the in-flight request can't pull the reader back.
export interface MessageJumperDeps {
	isLoaded(id: string): boolean
	hasMore(): boolean
	// Progress probes: a round that changes neither stops the loop.
	count(): number
	cursor(): unknown
	waitFirstPage(): Promise<void>
	waitIdle(): Promise<void>
	loadOlder(until: string): Promise<void>
	scrollTo(id: string): void
	// The turn shown as loading (null when nothing is pending).
	setPending(id: string | null): void
	// Lets the DOM render before the final scroll (nextTick in the page).
	beforeScroll?(): Promise<void>
	maxRounds?: number
}

export function createMessageJumper(d: MessageJumperDeps) {
	let target: string | null = null // latest requested turn not yet scrolled to
	let running = false
	return async function jump(id: string): Promise<void> {
		if (d.isLoaded(id) || !d.hasMore()) {
			target = null
			d.setPending(null)
			d.scrollTo(id)
			return
		}
		target = id
		d.setPending(id)
		if (running) return // the running load picks up the new target
		running = true
		try {
			await d.waitFirstPage()
			for (let round = 0; round < (d.maxRounds ?? 50); round++) {
				await d.waitIdle()
				const t = target
				if (!t || d.isLoaded(t) || !d.hasMore()) break
				const n = d.count()
				const c = d.cursor()
				await d.loadOlder(t)
				// No new rows and no cursor move: the server isn't advancing (or
				// the request failed) — stop instead of spinning.
				if (d.count() === n && d.cursor() === c) break
			}
		} finally {
			running = false
			d.setPending(null)
		}
		const t = target
		target = null
		if (!t) return // superseded by a click on a loaded turn (already scrolled)
		await d.beforeScroll?.()
		d.scrollTo(t)
	}
}
