import { createReadStream, existsSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { extname, join } from 'node:path'
import { Readable } from 'node:stream'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('.', import.meta.url))
const host = process.env.DEMO_HOST || '127.0.0.1'
const port = Number(process.env.DEMO_PORT || 4173)
const bowApiOrigin = process.env.BOW_API_ORIGIN || 'http://127.0.0.1:8000'

const contentTypes = {
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
}

// This demo serves a fixed, flat set of static assets from `root` (no
// subdirectories). Rather than sanitize the request path, we map it to an
// explicit allowlist whose values are absolute paths computed once at startup.
// Untrusted request input is therefore never used to build the filesystem
// path handed to createReadStream — it only selects a known-safe constant —
// which removes any possibility of path traversal.
const publicFiles = {
  'index.html': join(root, 'index.html'),
  'app.js': join(root, 'app.js'),
  'styles.css': join(root, 'styles.css'),
}

function safeStaticPath(pathname) {
  const requested = pathname === '/' || pathname === '/callback' ? 'index.html' : pathname.slice(1)
  return Object.prototype.hasOwnProperty.call(publicFiles, requested) ? publicFiles[requested] : null
}

async function proxyToBow(request, response) {
  const upstreamUrl = new URL(request.url.replace(/^\/bow/, ''), bowApiOrigin)
  const headers = new Headers()
  for (const [name, value] of Object.entries(request.headers)) {
    if (value && !['host', 'connection', 'content-length'].includes(name.toLowerCase())) {
      headers.set(name, Array.isArray(value) ? value.join(', ') : value)
    }
  }

  const hasBody = !['GET', 'HEAD'].includes(request.method || 'GET')
  const upstream = await fetch(upstreamUrl, {
    method: request.method,
    headers,
    body: hasBody ? Readable.toWeb(request) : undefined,
    duplex: hasBody ? 'half' : undefined,
    redirect: 'manual',
  })

  response.statusCode = upstream.status
  upstream.headers.forEach((value, name) => {
    if (!['content-encoding', 'content-length', 'transfer-encoding', 'connection'].includes(name.toLowerCase())) {
      response.setHeader(name, value)
    }
  })
  response.setHeader('Cache-Control', 'no-store')

  if (!upstream.body) {
    response.end()
    return
  }
  Readable.fromWeb(upstream.body).pipe(response)
}

const server = createServer(async (request, response) => {
  try {
    const url = new URL(request.url || '/', `http://${request.headers.host}`)
    if (url.pathname.startsWith('/bow/')) {
      await proxyToBow(request, response)
      return
    }

    const staticPath = safeStaticPath(url.pathname)
    if (!staticPath || !existsSync(staticPath) || !statSync(staticPath).isFile()) {
      response.statusCode = 404
      response.end('Not found')
      return
    }

    response.setHeader('Content-Type', contentTypes[extname(staticPath)] || 'application/octet-stream')
    response.setHeader('Cache-Control', 'no-store')
    response.setHeader(
      'Content-Security-Policy',
      "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'",
    )
    createReadStream(staticPath).pipe(response)
  } catch (error) {
    response.statusCode = 502
    response.setHeader('Content-Type', 'application/json')
    response.end(JSON.stringify({ error: error instanceof Error ? error.message : String(error) }))
  }
})

server.listen(port, host, () => {
  console.log(`Northstar Insights demo: http://${host}:${port}`)
  console.log(`Proxying BOW API requests to: ${bowApiOrigin}`)
})
