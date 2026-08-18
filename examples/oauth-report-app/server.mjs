import { createReadStream, existsSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { basename, extname, resolve, sep } from 'node:path'
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

// Assets live as flat files in this directory, so collapse any request to its
// basename before touching the filesystem. basename() strips every directory
// component (including "..") from attacker-controlled input, so traversal
// sequences like "/../../etc/passwd" degrade to a bare filename that can only
// resolve inside `root`. The resolved-within-root check below is defense in
// depth in case the served layout ever gains subdirectories.
const rootDir = resolve(root)
function safeStaticPath(pathname) {
  const requested = pathname === '/' || pathname === '/callback' ? 'index.html' : basename(pathname)
  if (!requested || requested === '.' || requested === '..') return null
  const resolved = resolve(rootDir, requested)
  return resolved === rootDir || resolved.startsWith(rootDir + sep) ? resolved : null
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
