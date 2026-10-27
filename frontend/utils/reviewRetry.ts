/**
 * Retry rule for the per-hunk review's accept/reject calls.
 *
 * Every resolve names the main build/version the reviewer was LOOKING AT, and
 * the server refuses (409) when main has moved since. That is the right
 * guard against approving on a stale picture — but main also moves whenever
 * ANY other instruction is accepted, so in a training session with several
 * cards the second card's "Accept all" failed on nothing but the first card's
 * accept. The card kept its stale ids, so clicking again failed the same way.
 *
 * The retry is safe only when, after a fresh load, the reviewer is still
 * looking at exactly what they clicked on: the same action units from the
 * same builds. Then the decision they made is the decision being sent, just
 * against the current main. Anything else (a unit gone, a unit added, a
 * different suggestion) shows the fresh state and leaves the decision to them.
 */

export interface ReviewUnit {
  build_id: string
  key: string
}

/** True for a 409 from the resolve routes (useMyFetch error shape). */
export function isReviewConflict(error: unknown): boolean {
  const e = error as any
  const status = e?.statusCode ?? e?.status ?? e?.response?.status
  return status === 409
}

/** Same (build, key) set, order-insensitive. Empty selections never match: a
 *  reload that found nothing to act on must not be resent. */
export function sameSelection(before: ReviewUnit[], after: ReviewUnit[]): boolean {
  if (!before.length || before.length !== after.length) return false
  const id = (u: ReviewUnit) => `${u.build_id}\u0000${u.key}`
  const a = new Set(before.map(id))
  if (a.size !== before.length) return false
  return after.every(u => a.has(id(u))) && new Set(after.map(id)).size === a.size
}
