// Per-embed frame policy for the chat document when Nuxt's own server serves
// the SPA (dev server and `node .output/server/index.mjs`). The production
// Docker image serves the SPA from FastAPI instead, where
// backend/app/routes/embed.py::frame_router does the same thing.
//
// `/embed/{id}` may be framed only by the embed's exact allowed origins (and
// this origin, for the settings preview). Every other page keeps the site's
// default policy; an unknown or disabled embed is never frameable.
import { defineEventHandler, getRequestURL, setResponseHeader } from 'h3'

const BACKEND = process.env.BOW_API_TARGET || 'http://127.0.0.1:8000'
const EMBED_PATH = /^\/embed\/([A-Za-z0-9_-]{4,40})\/?$/

export default defineEventHandler(async (event) => {
    if (event.method !== 'GET') return
    const url = getRequestURL(event)
    const match = EMBED_PATH.exec(url.pathname)
    if (!match) return
    const accept = event.headers.get('accept') || ''
    if (accept && !accept.includes('text/html')) return

    let csp = "frame-ancestors 'none'"
    try {
        const res = await fetch(`${BACKEND}/api/embed/${encodeURIComponent(match[1])}/bootstrap`, {
            headers: { accept: 'application/json' },
            signal: AbortSignal.timeout(3000),
        })
        if (res.ok) {
            const body: any = await res.json()
            const origins: string[] = Array.isArray(body?.allowed_origins) ? body.allowed_origins : []
            csp = `frame-ancestors 'self' ${origins.join(' ')}`.trim()
        }
    } catch {
        // Backend unreachable: fail closed (not frameable) rather than open.
    }
    setResponseHeader(event, 'Content-Security-Policy', `${csp}; object-src 'none'; base-uri 'self'`)
    setResponseHeader(event, 'Cache-Control', 'no-cache')
})
