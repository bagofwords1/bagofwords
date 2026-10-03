// Single entry point for "the user needs to authenticate against a
// connection." Decides between an inline credentials modal and a direct
// redirect to the provider's OAuth flow.
//
// Direct-redirect rule: when the connection is `user_required` AND `oauth`
// is the only auth mode the user can use, there's nothing meaningful to
// render in a modal — kick the OAuth flow immediately instead of flashing
// an empty "Sign in with X" button.
//
// Otherwise (multiple user auth modes, no OAuth available, missing
// connection ID, OAuth start fails) the caller falls back to opening the
// existing UserDataSourceCredentialsModal so the user can pick / type
// credentials manually.

interface ConnectionLike {
  id?: string
  type?: string
  auth_policy?: string
  allowed_user_auth_modes?: string[] | null
}

export interface SignInResult {
  // True if we initiated the OAuth redirect — the page is navigating away,
  // the caller should NOT open a modal as a follow-up.
  redirecting: boolean
  // Optional error string when redirecting=false because the OAuth call
  // failed; caller can surface it via toast / fall back to the modal.
  error?: string
}

// The backend explains *why* a sign-in could not start in the response body's
// `detail` ("... does not advertise a registration_endpoint", "Could not
// discover OAuth metadata for ...", a provider's own rejection). ofetch's
// FetchError.message is only `[GET] "<url>": 400`, so surfacing `message`
// alone throws that explanation away and leaves a bare status code on screen —
// the admin then has no way to tell a misconfigured URL from a server that
// simply doesn't support automatic registration.
export function connectionSignInError(e: any, fallback: string): string {
  const detail = (e as any)?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  // FastAPI request-validation errors put a list of {loc, msg} here instead.
  if (Array.isArray(detail)) {
    const msgs = detail.map((d: any) => d?.msg).filter(Boolean)
    if (msgs.length) return msgs.join('; ')
  }
  return e?.message || fallback
}

function isOAuthOnly(conn: ConnectionLike | null | undefined): boolean {
  if (!conn || conn.auth_policy !== 'user_required') return false
  const modes = conn.allowed_user_auth_modes
  if (!modes || modes.length === 0) return false
  // Only one mode and it's oauth → no user choice; redirect immediately.
  return modes.length === 1 && modes[0] === 'oauth'
}

export function useConnectionSignIn() {
  // Returns { redirecting: true } if we kicked the OAuth redirect, or
  // { redirecting: false, error? } if the caller should fall back to a
  // credentials modal.
  async function triggerUserSignIn(conn: ConnectionLike | null | undefined, opts?: { returnTo?: string }): Promise<SignInResult> {
    if (!conn?.id) return { redirecting: false, error: 'Connection has no id' }
    if (!isOAuthOnly(conn)) return { redirecting: false }

    try {
      // returnTo (app-internal path) rides in the signed OAuth state so the
      // callback lands the user back where they started — e.g. the
      // agent-creation tables step instead of /agents.
      const rt = opts?.returnTo && opts.returnTo.startsWith('/') ? `?return_to=${encodeURIComponent(opts.returnTo)}` : ''
      const { data, error } = await useMyFetch(`/connections/${conn.id}/oauth/authorize${rt}`, { method: 'GET' })
      if (error.value) throw error.value
      const result = data.value as any
      if (result?.authorization_url) {
        window.location.href = result.authorization_url
        return { redirecting: true }
      }
      return { redirecting: false, error: 'OAuth start did not return an authorization URL' }
    } catch (e: any) {
      return { redirecting: false, error: connectionSignInError(e, String(e)) }
    }
  }

  return { triggerUserSignIn, isOAuthOnly }
}
