import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { createServer, get } from 'node:http'
import { once } from 'node:events'
import test from 'node:test'
import ts from 'typescript'
import { createProxyServer } from '@refactorjs/http-proxy'

// Exercise the real Nuxt proxy configuration over HTTP. The upstream stands
// in for an OAuth provider URL builder; no identity provider is required.
const require = createRequire(import.meta.url)
const file = process.env.BOW_TEST_NUXT_CONFIG || new URL('../../nuxt.config.ts', import.meta.url)
const compiled = ts.transpileModule(readFileSync(file, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const exports = {}
new Function('require', 'exports', compiled)((id) => id === 'nuxt/config'
  ? { defineNuxtConfig: (config) => config } : require(id), exports)
const config = exports.default.proxy.proxies['/api']

test('OAuth upstream sees the incoming origin across ports and ingress proxies', async () => {
  const upstream = createServer((req, res) => {
    res.setHeader('Content-Type', 'application/json')
    res.end(JSON.stringify({ host: req.headers['x-forwarded-host'], proto: req.headers['x-forwarded-proto'] }))
  })
  upstream.listen(0, '127.0.0.1')
  await once(upstream, 'listening')
  const proxy = createProxyServer({
    target: `http://127.0.0.1:${upstream.address().port}`,
    changeOrigin: config.changeOrigin, xfwd: config.xfwd,
  })
  config.configure(proxy)
  const server = createServer((req, res) => proxy.web(req, res))
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  try {
    for (const scenario of [
      { headers: { host: 'localhost:3100' }, host: 'localhost:3100', proto: 'http' },
      { headers: { host: '127.0.0.1:4417' }, host: '127.0.0.1:4417', proto: 'http' },
      { headers: { host: 'bow.internal:3100', 'x-forwarded-host': 'bow.example.com', 'x-forwarded-proto': 'https' }, host: 'bow.example.com', proto: 'https' },
    ]) {
      const result = await new Promise((resolve, reject) => {
        get(`http://127.0.0.1:${server.address().port}/api/auth/entra/authorize`, { headers: scenario.headers }, (response) => {
          let body = ''
          response.setEncoding('utf8')
          response.on('data', (chunk) => { body += chunk })
          response.on('end', () => resolve(JSON.parse(body)))
          response.on('error', reject)
        }).on('error', reject)
      })
      assert.deepEqual(result, { host: scenario.host, proto: scenario.proto })
    }
  } finally {
    server.closeAllConnections()
    server.close()
    proxy.close()
    upstream.closeAllConnections()
    upstream.close()
  }
})
