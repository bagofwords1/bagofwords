// Timing and identity rules of an embedded artifact's session
// (EmbeddedArtifactGate.vue, useArtifactRuntime.ts). Pure, so they are tested
// on their own (tests/unit/embedSession.mjs).

// Renew this long before expiry, so a running request never straddles it.
export const RENEW_LEAD_MS = 5 * 60 * 1000
// Never renew more often than this, whatever the server said.
export const RENEW_MIN_MS = 15_000
// A failed renewal is retried after these delays (the last one repeats).
export const RENEW_RETRY_MS = [15_000, 30_000, 60_000, 120_000]
// A retry starts at least this long before expiry: asking the host for a
// credential may take up to 20 s, then the exchange itself.
export const RENEW_RETRY_MARGIN_MS = 30_000

/** Milliseconds the session has left, from `expires_in` (relative: no clock
 * or timezone involved), else from the `expires_at` instant. A bare
 * `expires_at` (no offset) is the server's UTC, never the browser's local time. */
export function sessionLifetimeMs(session: { expires_in?: number | null, expires_at?: string | null }, now = Date.now()): number {
  if (typeof session.expires_in === 'number' && Number.isFinite(session.expires_in)) return Math.max(0, session.expires_in * 1000)
  const text = String(session.expires_at || '')
  const at = Date.parse(/(Z|[+-]\d\d:?\d\d)$/i.test(text) ? text : `${text}Z`)
  return Number.isFinite(at) ? Math.max(0, at - now) : 0
}

/** When to renew a session that lasts `lifetimeMs`: RENEW_LEAD_MS before it
 * ends, or halfway through a shorter one. */
export function renewalDelayMs(lifetimeMs: number): number {
  if (!Number.isFinite(lifetimeMs)) return RENEW_MIN_MS
  return Math.max(lifetimeMs - Math.min(RENEW_LEAD_MS, lifetimeMs / 2), RENEW_MIN_MS)
}

/** After the `failures`-th failed renewal (0-based) with `remainingMs` of the
 * current session left: retry after a backoff, or, when no retry fits, end
 * the session when it really expires. */
export function renewalRetry(failures: number, remainingMs: number): { expire: boolean, delay: number } {
  const latest = remainingMs - RENEW_RETRY_MARGIN_MS
  if (latest <= 0) return { expire: true, delay: Math.max(0, remainingMs) }
  const backoff = RENEW_RETRY_MS[Math.min(failures, RENEW_RETRY_MS.length - 1)]
  return { expire: false, delay: Math.min(backoff, latest) }
}

/** Who the artifact runtime acts for; its ports are reset when this changes.
 * A renewed embed token is the same viewer (same host application,
 * organization and user, from the credential's userId or its viewer); anything
 * else, and a credential that names no user, is someone new. The app session
 * is keyed by being signed in at all: its token renewals must not reset the
 * ports (switching people goes through signing out). */
export function runtimeIdentity(embed: { token: string, organizationId: string, hostApplication: string, userId?: string | null, viewer: Record<string, any> | null } | null, appToken: string | null | undefined): string {
  if (!embed) return appToken ? 'app:signed-in' : 'anonymous'
  const user = embed.userId || embed.viewer?.id
  return user ? `embed:${embed.hostApplication}:${embed.organizationId}:${user}` : `embed-token:${embed.token}`
}

// Who BOW does not know (or knows twice, or deactivated) gains nothing from a retry.
export const PERMANENT_ERROR_CODES = ['embed.identity_unmapped', 'embed.identity_ambiguous', 'embed.user_inactive']

/** A sign-in or renewal failed with `errorCode`, after `failures` earlier
 * failed renewals, with `remainingMs` left of the current session (if any).
 * A failed renewal keeps the session it was renewing, and the app: retry
 * quietly until that session really ends. Without a session, or for a
 * permanent error, the gate fails (`retryable`: whether to offer a retry). */
export function renewalFailure({ hasCredential, errorCode, failures, remainingMs }: { hasCredential: boolean, errorCode?: string | null, failures: number, remainingMs: number }): { kind: 'retry' | 'expire' | 'fail', delay?: number, retryable?: boolean } {
  const permanent = PERMANENT_ERROR_CODES.includes(String(errorCode))
  if (!hasCredential || permanent) return { kind: 'fail', retryable: !permanent }
  const next = renewalRetry(failures, remainingMs)
  return { kind: next.expire ? 'expire' : 'retry', delay: next.delay }
}

// ── an artifact the embed cannot open ───────────────────────────────────────

/** Why an embedded artifact is not shown. Only its editors and admins are told
 * it is not published (the backend tells no one else); everyone else gets
 * `not_found`. `not_allowed`: the artifact's embed settings leave out this
 * host application. `failed`: the server broke or never answered -- the only
 * reason worth a retry. */
export type EmbedUnavailableReason = 'not_published' | 'not_found' | 'denied' | 'not_allowed' | 'failed'
/** What a failed load of the artifact means for the gate: a reason to show,
 * or a dead embed session (`session_invalid`), which ends like an expired one. */
export type EmbedLoadFailure = EmbedUnavailableReason | 'session_invalid'

/** The gate's reading of a failed GET /api/d/{ref} (HTTP status, error_code). */
export function embedLoadFailure(status?: number | null, errorCode?: string | null): EmbedLoadFailure {
  if (errorCode === 'dashboard.not_published') return 'not_published'
  if (errorCode === 'embed.not_allowed') return 'not_allowed'
  if (errorCode === 'embed.session_invalid' || status === 401) return 'session_invalid'
  if (status === 403) return 'denied'
  // No status (network, a thrown load, an empty body) or a server error.
  if (!status || status >= 500) return 'failed'
  return 'not_found'
}

/** A later request of the embedded view (its content, its queries) found the
 * embed session dead: it ends as an expired one does, not as a load failure. */
export function embedSessionEnded(status?: number | null, errorCode?: string | null): boolean {
  return status === 401 || errorCode === 'embed.session_invalid'
}

export const EMBED_UNAVAILABLE_MESSAGE_KEYS: Record<EmbedUnavailableReason, string> = {
  not_published: 'embed.unavailable.notPublished',
  not_found: 'embed.unavailable.notFound',
  denied: 'embed.unavailable.denied',
  not_allowed: 'embed.unavailable.notAllowed',
  failed: 'embed.unavailable.failed',
}

/** The English text of each reason, word for word as in locales/en.json
 * (tests/unit/embedSession.mjs pins the parity). Said when the catalog the
 * page was served lacks the key, so the viewer and the host never get a raw key. */
export const EMBED_UNAVAILABLE_FALLBACK_MESSAGES: Record<EmbedUnavailableReason, string> = {
  not_published: "This hasn't been published yet. It will appear here once it's published.",
  not_found: "This isn't available. It may not be published yet, or it was removed.",
  denied: "You don't have access to this. Ask its owner to share it with you.",
  not_allowed: "This isn't enabled for this application. An editor can allow it in its Embed settings in Bag of Words.",
  failed: "This couldn't be loaded. Try again in a moment.",
}

/** What the gate says for `reason`: the catalog text, else (t() answers the
 * key itself, or nothing) the built-in English. */
export function embedUnavailableMessage(reason: EmbedUnavailableReason, t: (key: string) => string): string {
  const key = EMBED_UNAVAILABLE_MESSAGE_KEYS[reason]
  const s = t(key)
  return s && s !== key ? s : EMBED_UNAVAILABLE_FALLBACK_MESSAGES[reason]
}
