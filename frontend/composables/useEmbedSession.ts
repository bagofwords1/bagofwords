// Embed session state for the chat iframe: bootstrap (public appearance),
// the session token (memory only — never localStorage, never a URL), renewal
// through a fresh host assertion, and an authenticated fetch for the
// /api/embed routes. The settings-page preview authenticates with the admin's
// own BOW session instead of a host assertion.

export type EmbedBootstrap = {
    embed_id: string
    name: string
    enabled: boolean
    allowed_origins: string[]
    branding: Record<string, any>
}

export type EmbedSessionInfo = {
    token: string
    expires_at: string
    user: { name?: string; email?: string }
    agents: { id: string; name: string }[]
    allow_agent_selection: boolean
    embed_id: string
}

export type AuthState = 'idle' | 'authenticating' | 'authenticated' | 'required' | 'failed' | 'disabled'

export class EmbedApiError extends Error {
    status: number
    code: string
    params: Record<string, any>
    constructor(status: number, code: string, message: string, params: Record<string, any> = {}) {
        super(message)
        this.status = status
        this.code = code
        this.params = params
    }
}

// Renew this long before expiry so a running stream never straddles it.
const RENEW_LEAD_MS = 5 * 60 * 1000

export function useEmbedSession(embedId: string, baseURL: string) {
    const bootstrap = ref<EmbedBootstrap | null>(null)
    const session = ref<EmbedSessionInfo | null>(null)
    const authState = ref<AuthState>('idle')
    const authError = ref<string>('')
    let token = ''
    let renewTimer: ReturnType<typeof setTimeout> | null = null
    // Supplied by the page: how to obtain a fresh assertion (host) or a preview
    // session (admin). Resolves to a session payload.
    let authenticator: (() => Promise<EmbedSessionInfo>) | null = null

    const api = (path: string) => `${baseURL}/embed/${encodeURIComponent(embedId)}${path}`

    async function parseError(res: Response): Promise<EmbedApiError> {
        let body: any = null
        try { body = await res.json() } catch { /* not json */ }
        return new EmbedApiError(res.status, body?.error_code || `http.${res.status}`, body?.detail || res.statusText, body?.params || {})
    }

    async function loadBootstrap(): Promise<EmbedBootstrap> {
        const res = await fetch(api('/bootstrap'), { credentials: 'omit' })
        if (!res.ok) throw await parseError(res)
        bootstrap.value = await res.json()
        if (!bootstrap.value!.enabled) authState.value = 'disabled'
        return bootstrap.value!
    }

    function setAuthenticator(fn: () => Promise<EmbedSessionInfo>) { authenticator = fn }

    function applySession(info: EmbedSessionInfo) {
        token = info.token
        session.value = { ...info, token: '' } // the token never leaves this closure
        authState.value = 'authenticated'
        authError.value = ''
        scheduleRenewal(info.expires_at)
    }

    function scheduleRenewal(expiresAt: string) {
        if (renewTimer) clearTimeout(renewTimer)
        const at = new Date(expiresAt).getTime() - RENEW_LEAD_MS - Date.now()
        renewTimer = setTimeout(() => { authenticate().catch(() => { /* state already set */ }) }, Math.max(at, 15_000))
    }

    let inflight: Promise<boolean> | null = null
    async function authenticate(): Promise<boolean> {
        if (inflight) return inflight
        inflight = (async () => {
            if (!authenticator) { authState.value = 'required'; return false }
            authState.value = 'authenticating'
            try {
                applySession(await authenticator())
                return true
            } catch (e: any) {
                token = ''
                session.value = null
                if (e instanceof EmbedApiError && e.code === 'embed.disabled') authState.value = 'disabled'
                else authState.value = 'failed'
                authError.value = e?.code || e?.message || 'auth_failed'
                return false
            } finally {
                inflight = null
            }
        })()
        return inflight
    }

    // Exchange a host assertion for a session (used by the authenticator).
    async function exchangeAssertion(assertion: string): Promise<EmbedSessionInfo> {
        const res = await fetch(api('/session'), {
            method: 'POST', credentials: 'omit',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ assertion }),
        })
        if (!res.ok) throw await parseError(res)
        return res.json()
    }

    /**
     * fetch() with the embed bearer. A 401 means the session died (expiry,
     * revocation, disable): re-authenticate once and retry; if that fails the
     * caller sees the error and the UI shows "sign in required".
     */
    async function authedFetch(path: string, init: RequestInit = {}, retry = true): Promise<Response> {
        if (!token) {
            const ok = await authenticate()
            if (!ok) throw new EmbedApiError(401, 'embed.session_invalid', 'Not signed in')
        }
        const headers = new Headers(init.headers || {})
        headers.set('Authorization', `Bearer ${token}`)
        const res = await fetch(api(path), { ...init, headers, credentials: 'omit' })
        if (res.status === 401 && retry) {
            token = ''
            const ok = await authenticate()
            if (ok) return authedFetch(path, init, false)
            authState.value = authState.value === 'disabled' ? 'disabled' : 'required'
            throw await parseError(res)
        }
        if (!res.ok) throw await parseError(res)
        return res
    }

    async function refreshSession() {
        const res = await authedFetch('/session')
        const info: EmbedSessionInfo = await res.json()
        session.value = { ...info, token: '' }
        return info
    }

    async function logout(): Promise<void> {
        if (renewTimer) clearTimeout(renewTimer)
        renewTimer = null
        const had = token
        token = ''
        session.value = null
        authState.value = 'idle'
        if (had) {
            try {
                await fetch(api('/session'), { method: 'DELETE', credentials: 'omit', headers: { Authorization: `Bearer ${had}` } })
            } catch { /* best effort; the token is gone from memory regardless and expires server-side */ }
        }
    }

    function dispose() {
        if (renewTimer) clearTimeout(renewTimer)
        renewTimer = null
        token = ''
    }

    return {
        bootstrap, session, authState, authError,
        loadBootstrap, setAuthenticator, authenticate, exchangeAssertion, authedFetch, refreshSession, logout, dispose,
        get hasToken() { return !!token },
    }
}
