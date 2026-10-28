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
 * looking at what they clicked on. What "still looking at" means differs by
 * call site, so the two guards below differ:
 *   - a single hunk accept/reject needs only that one unit to still be present
 *     (selectionStillValid) — other units appearing or vanishing doesn't change
 *     what that one click meant;
 *   - "Accept/Reject all" submits the whole on-screen set, so it needs that set
 *     unchanged (sameSelection), extras included, or it defers to the reviewer.
 */

export interface ReviewUnit {
  build_id: string
  key: string
}

/**
 * The backend's machine-readable discriminator for the one 409 the retry is
 * for: the main build/version moved under the card (ErrorCode.REVIEW_STALE_MAIN
 * in backend/app/errors/codes.py). The resolve routes ALSO return 409 for a
 * selection no refresh can fix (resource.conflict) — reloading finds the same
 * bad selection and resends a request guaranteed to 409 again, so that case
 * must not be retried. Match the code, not the status or the human-readable
 * detail (prose that gets localized), to tell the two apart.
 */
export const REVIEW_STALE_MAIN_CODE = 'review.stale_main'

/** The error body's `error_code`, across the shapes useMyFetch/ofetch surface. */
function errorCode(error: unknown): string | undefined {
  const e = error as any
  return e?.data?.error_code ?? e?.response?._data?.error_code
}

/** True only for the stale-main 409 (retryable), not the invalid-selection 409. */
export function isReviewConflict(error: unknown): boolean {
  const e = error as any
  const status = e?.statusCode ?? e?.status ?? e?.response?.status
  return status === 409 && errorCode(error) === REVIEW_STALE_MAIN_CODE
}

const unitId = (u: ReviewUnit) => `${u.build_id}\u0000${u.key}`

/** Same (build, key) set, order-insensitive — the guard for "Accept/Reject all",
 *  whose request is the whole on-screen set. Empty selections never match: a
 *  reload that found nothing to act on must not be resent. */
export function sameSelection(before: ReviewUnit[], after: ReviewUnit[]): boolean {
  if (!before.length || before.length !== after.length) return false
  const a = new Set(before.map(unitId))
  if (a.size !== before.length) return false
  return after.every(u => a.has(unitId(u))) && new Set(after.map(unitId)).size === a.size
}

/** Every unit the reviewer acted on is still present after a reload — the guard
 *  for a single hunk accept/reject, where units that appeared or vanished don't
 *  change what the one click meant. Empty never matches (nothing to resend). */
export function selectionStillValid(units: ReviewUnit[], onScreen: ReviewUnit[]): boolean {
  if (!units.length) return false
  const present = new Set(onScreen.map(unitId))
  return units.every(u => present.has(unitId(u)))
}
