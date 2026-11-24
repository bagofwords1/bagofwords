// A sign-in or sign-up reached from an embedded artifact's sign-in window
// (redirect=/embed-signin?host=host_…) says which host application it
// belongs to, so an account it creates gets that application's new-user role
// (an app user by default). See backend/app/core/workspace_access.py.
//
// The intent belongs to ONE request, never to the browser: the form asks for
// a token as it submits and sends it in that request's `X-Embed-Signup`
// header; a provider button passes `?embed_host=` to its authorization. An
// abandoned window therefore leaves nothing behind for a later, ordinary
// sign-in. A failure only means the account is created as an ordinary one.

const EMBED_SIGNIN = /^\/embed-signin\?(?:[^#]*&)?host=(host_[a-f0-9]{8,32})(?:&|$)/

export function embedHostOfRedirect(redirect: unknown): string | null {
  if (typeof redirect !== 'string') return null
  return EMBED_SIGNIN.exec(redirect)?.[1] || null
}

// Headers for the registration or sign-in request being submitted now.
export async function embedSignupHeaders(redirect: unknown): Promise<Record<string, string>> {
  const host = embedHostOfRedirect(redirect)
  if (!host) return {}
  try {
    const { token } = await $fetch<{ token: string }>(`/api/embed/${host}/sso/intent`, { method: 'POST' })
    return token ? { 'X-Embed-Signup': token } : {}
  } catch {
    return {}
  }
}
