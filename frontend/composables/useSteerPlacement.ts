/**
 * Where a steering message ("Send now" while the agent runs) renders inside
 * its turn's block stream.
 *
 * Pure functions — no Vue reactivity — shared verbatim by the report view and
 * the public share view, and unit-tested in tests/unit/useSteerPlacement.mjs.
 *
 * Policy: every steer gets exactly one slot.
 * - Picked up (the agent stamped `steering_loop_index`): right above the first
 *   block whose `loop_index` is >= it — the first step that acted on the
 *   steer. When that step has not arrived yet, after the stream tail.
 * - Pending (or written before the stamp existed): by send time — above the
 *   first block that started at or after the steer was sent, else the tail.
 * - A turn with no blocks places nothing; the page shows such steers above
 *   the turn itself.
 */

export interface SteerLike {
  id: string | number
  created_at?: string | null
  steering_loop_index?: number | null
  steering_applied_at?: string | null
  steering_applied?: boolean
}

export interface BlockLike {
  loop_index?: number | null
  started_at?: string | null
  created_at?: string | null
  _client_arrived_at?: string | null
}

export interface SteerPlacement<S> {
  /** visible block index -> steers rendered right above that block */
  before: Map<number, S[]>
  /** steers rendered after the last block */
  tail: S[]
}

const ts = (v: string | null | undefined): number => (v ? new Date(v).getTime() : 0)

// Server timestamps first; fall back to the client arrival stamp so a freshly
// streamed skeleton block (timestamps not yet serialized) never compares as
// epoch-0 "older than the steer".
const blockStart = (b: BlockLike): number =>
  ts(b?.started_at || b?.created_at || b?._client_arrived_at)

const hasLoopIndex = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** Index of the block a steer renders above, or -1 for the tail. */
export function steerSlot(steer: SteerLike, blocks: BlockLike[]): number {
  const loop = steer.steering_loop_index
  if (hasLoopIndex(loop)) {
    return blocks.findIndex(b => hasLoopIndex(b?.loop_index) && (b.loop_index as number) >= loop)
  }
  const sent = ts(steer.created_at)
  return blocks.findIndex(b => sent <= blockStart(b))
}

export function placeSteers<S extends SteerLike>(steers: S[], blocks: BlockLike[]): SteerPlacement<S> {
  const before = new Map<number, S[]>()
  const tail: S[] = []
  if (!steers.length || !blocks.length) return { before, tail }
  for (const s of steers) {
    const slot = steerSlot(s, blocks)
    if (slot < 0) {
      tail.push(s)
    } else {
      const list = before.get(slot)
      list ? list.push(s) : before.set(slot, [s])
    }
  }
  return { before, tail }
}

/** Steering rows of a conversation, grouped by the system completion they
 *  were sent into (one pass over the rows). */
export function groupSteersByParent<S extends SteerLike & { role?: string; message_type?: string | null; parent_id?: string | null }>(
  rows: S[],
): Map<string, S[]> {
  const out = new Map<string, S[]>()
  for (const r of rows) {
    if (r?.role !== 'user' || r.message_type !== 'steering' || !r.parent_id) continue
    const key = String(r.parent_id)
    const list = out.get(key)
    list ? list.push(r) : out.set(key, [r])
  }
  return out
}

/** The agent has picked the steer up — live (SSE ack) or after a reload. */
export function isSteerApplied(steer: SteerLike): boolean {
  return !!(steer.steering_applied || steer.steering_applied_at || hasLoopIndex(steer.steering_loop_index))
}
