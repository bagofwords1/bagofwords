// Frame policy for a published artifact inside a host application,
// /d/{ref}?host={host application}, when Nuxt's own server serves the SPA
// (the dev server, `node .output/server/index.mjs`). The Docker image serves
// the SPA from FastAPI, where backend/app/routes/embed.py::frame_router does
// the same.
//
// The page may be framed by this origin and by the host application's exact
// origins, and only when the artifact allows that host. Pages without ?host=
// keep the site's default policy.
import { defineEventHandler, getRequestURL, setResponseHeader } from 'h3'

const BACKEND = process.env.BOW_API_TARGET || 'http://127.0.0.1:8000'
const DASHBOARD_PATH = /^\/d\/([^/]{1,200})\/?$/
const HOST_ID = /^host_[a-f0-9]{8,32}$/

export default defineEventHandler(async (event) => {
  if (event.method !== 'GET') return
  const url = getRequestURL(event)
  const match = DASHBOARD_PATH.exec(url.pathname)
  const host = url.searchParams.get('host')
  if (!match || !host) return
  let ancestors = "'self'"
  if (HOST_ID.test(host)) {
    try {
      const res = await fetch(
        `${BACKEND}/api/embed/${host}/frame?artifact=${encodeURIComponent(decodeURIComponent(match[1]))}`,
        { headers: { accept: 'application/json' }, signal: AbortSignal.timeout(3000) },
      )
      if (res.ok) {
        const body: any = await res.json()
        if (typeof body?.frame_ancestors === 'string') ancestors = body.frame_ancestors
      }
    } catch {
      // Backend unreachable: this origin only (fail closed).
    }
  }
  setResponseHeader(event, 'Content-Security-Policy', `frame-ancestors ${ancestors}; object-src 'none'; base-uri 'self'`)
  setResponseHeader(event, 'Cache-Control', 'no-cache')
})
