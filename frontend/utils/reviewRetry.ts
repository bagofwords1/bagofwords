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
 * looking at EXACTLY what they clicked on — same hunk AND same rendered text.
 * The (build_id, key) pair alone is not enough: a hunk key is hashed from the
 * suggestion's frozen (base, proposed) pair and is deliberately STABLE as main
 * drifts (see backend text_hunks.Hunk.key), while the lenient
 * rebased_hunks_against_main re-surfaces a drifted hunk with `before` taken from
 * main's CURRENT text. So the same key can describe different on-screen text
 * after a reload, and a key-only guard would let the retry apply a hunk over
 * text the reviewer never saw — routing around the server guard with a token
 * built to ignore drift. The guards therefore compare the rendered CONTENT
 * (`before`/`after`) too, not just identity. They differ by call site:
 *   - a single hunk accept/reject needs only that one unit still present with
 *     the same content (selectionStillValid) — other units appearing or
 *     vanishing doesn't change what that one click meant;
 *   - "Accept/Reject all" submits the whole on-screen set, so it needs that set
 *     unchanged (sameSelection), extras and content included, or it defers to
 *     the reviewer.
 */

export interface ReviewUnit {
  build_id: string
  key: string
  // The text the reviewer saw for this unit at click time: `before` is what the
  // hunk removes (taken from main's current region for a drifted hunk), `after`
  // what it adds. Carried so the guards can detect that the same key now renders
  // DIFFERENT text after a reload. Field changes bind their value into the key,
  // so these stay undefined for them and identity alone suffices.
  before?: string
  after?: string
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

/** Full identity of a unit INCLUDING its rendered text, so a drifted hunk (same
 *  key, different `before`) does not match what the reviewer acted on. */
const unitSig = (u: ReviewUnit) =>
  `${u.build_id}\u0000${u.key}\u0000${u.before ?? ''}\u0000${u.after ?? ''}`

/** Same set of units AND same rendered text, order-insensitive — the guard for
 *  "Accept/Reject all", whose request is the whole on-screen set. Empty
 *  selections never match: a reload that found nothing to act on must not be
 *  resent. A hunk whose text drifted changes its signature, so the set is no
 *  longer equal and the retry is declined. */
export function sameSelection(before: ReviewUnit[], after: ReviewUnit[]): boolean {
  if (!before.length || before.length !== after.length) return false
  const a = new Set(before.map(unitSig))
  if (a.size !== before.length) return false
  return after.every(u => a.has(unitSig(u))) && new Set(after.map(unitSig)).size === a.size
}

/** Every unit the reviewer acted on is still present after a reload WITH THE
 *  SAME rendered text — the guard for a single hunk accept/reject, where units
 *  that appeared or vanished don't change what the one click meant, but a change
 *  to the clicked hunk's own text does. Empty never matches (nothing to
 *  resend). */
export function selectionStillValid(units: ReviewUnit[], onScreen: ReviewUnit[]): boolean {
  if (!units.length) return false
  const present = new Set(onScreen.map(unitSig))
  return units.every(u => present.has(unitSig(u)))
}
