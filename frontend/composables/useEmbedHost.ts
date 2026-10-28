// The iframe side of the embed handshake (the SDK side lives in
// backend/app/static/embed/bow-embed.js — keep the two in step).
//
// Trust rules, all enforced here and nowhere else in the page:
//   * only `window.parent` may talk to us, and only from an origin the embed
//     lists in `allowed_origins` (or our own origin, for the settings preview);
//   * the first accepted message is `bow:init`, which carries the nonce every
//     later message must repeat; the parent's origin is bound at that moment
//     and never changes for the life of the frame;
//   * we post to the parent with its exact origin as targetOrigin — never '*'
//     — except the credential-free `bow:hello`, sent before the origin is known
//     so the parent knows the frame is listening.
// The host is a UI peer, not an authority: nothing it sends can change what
// the backend lets this user do.

export type HostInit = {
    nonce: string
    mode: 'floating' | 'inline'
    context?: Record<string, any> | null
    metadata?: Record<string, any> | null
    locale?: string | null
    theme?: 'light' | 'dark' | null
    open?: boolean
}

type Handlers = {
    onInit: (init: HostInit) => void
    onAssertion: (requestId: string, assertion: string | null, error?: string) => void
    onContext: (context: Record<string, any> | null) => void
    onMetadata: (metadata: Record<string, any> | null) => void
    onOpen: (open: boolean) => void
    onLogout: () => void
    onLocale: (locale: string | null) => void
}

export function useEmbedHost(allowedOrigins: () => string[], handlers: Handlers) {
    let hostOrigin: string | null = null
    let nonce: string | null = null
    let listening = false
    const embedded = typeof window !== 'undefined' && window.parent && window.parent !== window

    function originAllowed(origin: string): boolean {
        if (origin === window.location.origin) return true // settings preview
        return allowedOrigins().includes(origin)
    }

    function onMessage(event: MessageEvent) {
        if (!embedded || event.source !== window.parent) return
        const data = event.data
        if (!data || typeof data !== 'object' || typeof data.type !== 'string' || !data.type.startsWith('bow:')) return
        if (hostOrigin === null) {
            if (data.type !== 'bow:init') return
            if (!originAllowed(event.origin)) {
                console.warn('[bow-embed] init refused from origin', event.origin)
                return
            }
            if (typeof data.nonce !== 'string' || data.nonce.length < 8) return
            hostOrigin = event.origin
            nonce = data.nonce
            handlers.onInit({
                nonce: data.nonce,
                mode: data.mode === 'inline' ? 'inline' : 'floating',
                context: plainObject(data.context),
                metadata: plainObject(data.metadata),
                locale: typeof data.locale === 'string' ? data.locale : null,
                theme: data.theme === 'dark' || data.theme === 'light' ? data.theme : null,
                open: !!data.open,
            })
            post('bow:ready', {})
            return
        }
        if (event.origin !== hostOrigin || data.nonce !== nonce) return
        switch (data.type) {
            case 'bow:assertion':
                handlers.onAssertion(String(data.requestId || ''), typeof data.assertion === 'string' ? data.assertion : null, undefined)
                break
            case 'bow:assertion-error':
                handlers.onAssertion(String(data.requestId || ''), null, String(data.message || 'assertion failed'))
                break
            case 'bow:context':
                handlers.onContext(plainObject(data.context))
                break
            case 'bow:metadata':
                handlers.onMetadata(plainObject(data.metadata))
                break
            case 'bow:open':
                handlers.onOpen(true)
                break
            case 'bow:close':
                handlers.onOpen(false)
                break
            case 'bow:logout':
                handlers.onLogout()
                break
            case 'bow:locale':
                handlers.onLocale(typeof data.locale === 'string' ? data.locale : null)
                break
            case 'bow:init':
                // A re-init (SDK re-sent after a late hello): accept only from the bound host.
                handlers.onContext(plainObject(data.context))
                handlers.onMetadata(plainObject(data.metadata))
                post('bow:ready', {})
                break
        }
    }

    function plainObject(value: any): Record<string, any> | null {
        if (value && typeof value === 'object' && !Array.isArray(value)) {
            try { return JSON.parse(JSON.stringify(value)) } catch { return null }
        }
        return null
    }

    function post(type: string, payload: Record<string, any>) {
        if (!embedded || !hostOrigin || !nonce) return
        try { window.parent.postMessage({ type, nonce, ...payload }, hostOrigin) } catch { /* detached */ }
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
    }

    return {
        embedded,
        start,
        stop,
        post,
        get hostOrigin() { return hostOrigin },
    }
}
