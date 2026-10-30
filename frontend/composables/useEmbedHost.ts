// The frame side of the embed handshake. The host side is the SDK served at
// /api/embed/sdk/v1/bow-embed.js (backend/app/static/embed/bow-embed.js);
// keep the two in step.
//
// Trust rules, all enforced here:
//   * only `window.parent` may talk to us, and only from an origin the host
//     application lists in `allowed_origins`;
//   * the first accepted message is `bow:init`, carrying the nonce every later
//     message must repeat; the parent's origin is bound then and never changes;
//   * we post to the parent with its exact origin as targetOrigin, never '*',
//     except the credential-free `bow:hello` sent before the origin is known.
// The host is a UI peer, not an authority: what it sends (context, theme)
// cannot change what the backend lets this user do.

export type HostInit = {
  context: Record<string, any> | null
  locale: string | null
  theme: 'light' | 'dark' | null
}

type Handlers = {
  onInit: (init: HostInit) => void
  onContext: (context: Record<string, any> | null) => void
  onLocale: (locale: string | null) => void
  onLogout: () => void
}

function plainObject(value: any): Record<string, any> | null {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    try { return JSON.parse(JSON.stringify(value)) } catch { return null }
  }
  return null
}

export function useEmbedHost(allowedOrigins: () => string[], handlers: Handlers) {
  let hostOrigin: string | null = null
  let nonce: string | null = null
  let listening = false
  const pending = new Map<string, { resolve: (v: string) => void, reject: (e: Error) => void }>()
  const embedded = typeof window !== 'undefined' && window.parent && window.parent !== window

  function onMessage(event: MessageEvent) {
    if (!embedded || event.source !== window.parent) return
    const data = event.data
    if (!data || typeof data !== 'object' || typeof data.type !== 'string' || !data.type.startsWith('bow:')) return
    if (hostOrigin === null) {
      if (data.type !== 'bow:init') return
      if (!allowedOrigins().includes(event.origin)) {
        console.warn('[bow-embed] init refused from origin', event.origin)
        return
      }
      if (typeof data.nonce !== 'string' || data.nonce.length < 16) return
      hostOrigin = event.origin
      nonce = data.nonce
      handlers.onInit({
        context: plainObject(data.context),
        locale: typeof data.locale === 'string' ? data.locale : null,
        theme: data.theme === 'dark' || data.theme === 'light' ? data.theme : null,
      })
      return
    }
    if (event.origin !== hostOrigin || data.nonce !== nonce) return
    switch (data.type) {
      case 'bow:credential': {
        const waiter = pending.get(String(data.requestId || ''))
        if (!waiter) return
        pending.delete(String(data.requestId))
        if (typeof data.credential === 'string' && data.credential) waiter.resolve(data.credential)
        else waiter.reject(new Error(String(data.error || 'The host application did not sign you in')))
        break
      }
      case 'bow:context':
        handlers.onContext(plainObject(data.context))
        break
      case 'bow:locale':
        handlers.onLocale(typeof data.locale === 'string' ? data.locale : null)
        break
      case 'bow:logout':
        handlers.onLogout()
        break
    }
  }

  function post(type: string, payload: Record<string, any> = {}) {
    if (!embedded || !hostOrigin || !nonce) return
    try { window.parent.postMessage({ ...payload, type, nonce }, hostOrigin) } catch { /* detached */ }
  }

  /** Ask the host for a fresh credential ('assertion' or 'id_token'). */
  function requestCredential(kind: 'assertion' | 'id_token', timeoutMs = 20000): Promise<string> {
    const requestId = crypto.randomUUID()
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(requestId)
        reject(new Error('The host application did not answer'))
      }, timeoutMs)
      pending.set(requestId, {
        resolve: (v) => { clearTimeout(timer); resolve(v) },
        reject: (e) => { clearTimeout(timer); reject(e) },
      })
      post('bow:credential-request', { requestId, kind })
    })
  }

  function start() {
    if (listening || !embedded) return
    listening = true
    window.addEventListener('message', onMessage)
    // Credential-free: tells the SDK we are listening so it (re)sends init.
    try { window.parent.postMessage({ type: 'bow:hello' }, '*') } catch { /* no parent */ }
  }

  function stop() {
    if (!listening) return
    listening = false
    window.removeEventListener('message', onMessage)
    for (const waiter of pending.values()) waiter.reject(new Error('closed'))
    pending.clear()
  }

  return { embedded, start, stop, post, requestCredential, get bound() { return hostOrigin !== null } }
}
