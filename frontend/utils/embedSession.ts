// Session lifecycle helpers for an artifact embedded in a host application
// (EmbeddedArtifactGate.vue, useArtifactRuntime.ts). Pure, so they are tested
// directly (tests/unit/embedSession.mjs).

/** Milliseconds since epoch for a session expiry. The server sends UTC; a
 *  timestamp without a zone is read as UTC too, never as the browser's local
 *  time (which would renew hours late west of UTC and loop east of it). */
export function expiryMs(value: string | number | null | undefined): number {
  if (typeof value === 'number') return value
  const text = String(value || '').trim()
  if (!text) return NaN
  const zoned = /([zZ]|[+-]\d{2}:?\d{2})$/.test(text) ? text : `${text}Z`
  return Date.parse(zoned)
}

/** How long to wait before renewing: `lead` before expiry, never sooner than `floor`. */
export function renewDelay(expiresAt: string | number, now: number, lead: number, floor = 15_000): number {
  const at = expiryMs(expiresAt)
  if (!Number.isFinite(at)) return floor
  return Math.max(at - now - lead, floor)
}

/** Invalidates in-flight sign-ins: each attempt takes a ticket, and a logout
 *  or unmount makes every older ticket stale, so a late answer is discarded
 *  (and its session revoked) instead of signing the previous user back in. */
export function createAuthGuard() {
  let generation = 0
  return {
    ticket: () => generation,
    isCurrent: (ticket: number) => ticket === generation,
    invalidate: () => { generation += 1 },
  }
}

/** Who the artifact runtime acts as. The runtime's channels are reset when
 *  this changes (signed out, another person), not when the same person's
 *  token is renewed: every request reads the current token anyway. An app
 *  session is keyed by being signed in at all (switching people goes through
 *  signing out, and the user record loads after the token, which must not
 *  reset a channel that just connected). */
export function runtimeIdentity(
  embed: { userId?: string | null, organizationId?: string, hostApplication?: string, token?: string } | null,
  appToken: string | null | undefined,
): string {
  if (embed) return `embed:${embed.hostApplication || ''}:${embed.organizationId || ''}:${embed.userId || embed.token || ''}`
  return appToken ? 'app:signed-in' : 'anonymous'
}
