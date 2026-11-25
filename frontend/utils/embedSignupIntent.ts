// A sign-in or sign-up reached from an embedded artifact's sign-in window
// (redirect=/embed-signin?host=host_…) says which host application it
// belongs to, so an account it creates gets that application's new-user role
// (an app user by default). See backend/app/core/workspace_access.py.
//
// The intent belongs to ONE request, never to the browser: the form asks for
// a token as it submits and sends it in that request's `X-Embed-Signup`
// header; a provider button passes `?embed_host=` to its authorization.
//
// Fail closed. Inside a host application's window, a request that can create
// an account is NOT sent without its intent: if the token cannot be had (rate
// limit, network, a disabled application) the submit stops and the person
// retries. Sending it bare would create an ordinary member where the host
// application says app user.

const EMBED_SIGNIN = /^\/embed-signin\?(?:[^#]*&)?host=(host_[a-f0-9]{8,32})(?:&|$)/

export class EmbedIntentError extends Error {
  cause?: unknown

  constructor(cause?: unknown) {
    super('The host application could not be confirmed for this sign-in')
    this.name = 'EmbedIntentError'
    this.cause = cause
  }
}

export function embedHostOfRedirect(redirect: unknown): string | null {
  if (typeof redirect !== 'string') return null
  return EMBED_SIGNIN.exec(redirect)?.[1] || null
}

type Post = (url: string) => Promise<{ token?: string } | null | undefined>

// Headers for the one request being submitted now: none outside a window,
// the intent inside one, and an EmbedIntentError when it cannot be had.
export async function embedSignupHeaders(redirect: unknown, post: Post): Promise<Record<string, string>> {
  const host = embedHostOfRedirect(redirect)
  if (!host) return {}
  let token: string | undefined
  try {
    token = (await post(`/api/embed/${host}/sso/intent`))?.token
  } catch (error) {
    throw new EmbedIntentError(error)
  }
  if (!token) throw new EmbedIntentError()
  return { 'X-Embed-Signup': token }
}

// Run `send` with those headers. `send` is not called at all when the intent
// was needed and could not be had.
export async function sendWithEmbedIntent<T>(
  redirect: unknown, post: Post, send: (headers: Record<string, string>) => Promise<T>,
): Promise<T> {
  return send(await embedSignupHeaders(redirect, post))
}
