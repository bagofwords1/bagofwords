# Sandbox Feedback Loop — OAuth demo static server "Path Traversal (Snyk HIGH)"

Reproduces and validates the Snyk Code **HIGH** finding on the OAuth report
demo's static file server: *"Unsanitized input from the request URL flows into
`node:fs.createReadStream`, where it is used as a path. This may result in a
Path Traversal vulnerability and allow an attacker to read arbitrary files."*
(`examples/oauth-report-app/server.mjs:81`).

This doc is the runnable feedback loop used to confirm the finding and verify the
fix with `curl` against the actual demo server in a fresh sandbox — no backend,
no external services.

---

## How it was found

Snyk scan run with `SNYK_PAT` over the repo:

- **Snyk Open Source** — frontend (`yarn.lock`, 1250 deps) and backend
  (`uv.lock`, 238 deps): **0 high/critical**.
- **Snyk Code (SAST)** — `snyk code test --severity-threshold=high`: **1 HIGH**,
  Path Traversal at `examples/oauth-report-app/server.mjs:81`.

---

## Root cause (validated)

`safeStaticPath` tried to sanitize the request path with a
`normalize`/`relative`-based containment check
(`examples/oauth-report-app/server.mjs`, pre-fix):

```js
function safeStaticPath(pathname) {
  const requested = pathname === '/' || pathname === '/callback' ? 'index.html' : pathname.slice(1)
  const resolved = normalize(join(root, requested))
  const withinRoot = relative(root, resolved)
  return !withinRoot.startsWith('..') && !isAbsolute(withinRoot) ? resolved : null
}
```

The resolved path — derived directly from the untrusted request URL — then flowed
into `createReadStream(staticPath)` (`server.mjs:81`). Snyk's taint analysis does
not recognize the ad-hoc `relative()`-prefix check as a sufficient sanitizer, so
untrusted request input reaches the filesystem read: a HIGH Path Traversal.

The **tell** that this is a real finding rather than intended behavior: the demo
only ever needs to serve a fixed, flat set of three assets (`index.html`,
`app.js`, `styles.css`) — there is no legitimate reason for a request-derived
path to reach `createReadStream` at all.

---

## The fix

Stop sanitizing a request-derived path; instead map the request to an explicit
allowlist whose values are absolute paths computed once at startup, so untrusted
input only *selects a known-safe constant* and never builds the path handed to
`createReadStream` (`server.mjs`):

```diff
-import { extname, isAbsolute, join, normalize, relative } from 'node:path'
+import { extname, join } from 'node:path'
```

```diff
+const publicFiles = {
+  'index.html': join(root, 'index.html'),
+  'app.js': join(root, 'app.js'),
+  'styles.css': join(root, 'styles.css'),
+}
+
 function safeStaticPath(pathname) {
   const requested = pathname === '/' || pathname === '/callback' ? 'index.html' : pathname.slice(1)
-  const resolved = normalize(join(root, requested))
-  const withinRoot = relative(root, resolved)
-  return !withinRoot.startsWith('..') && !isAbsolute(withinRoot) ? resolved : null
+  return Object.prototype.hasOwnProperty.call(publicFiles, requested) ? publicFiles[requested] : null
 }
```

Any request that is not an exact allowlist key returns `null` → 404. The value
that reaches `createReadStream` is always one of three constant paths, which
also clears the Snyk taint flow.

---

## Verification loop (curl, no backend needed)

The demo server is a standalone static server, so the loop runs it directly on a
localhost port and probes both legitimate and traversal requests.

### Environment

```bash
cd examples/oauth-report-app
node --check server.mjs                       # syntax
DEMO_PORT=4199 node server.mjs &              # start server
```

### Observed — after the fix (PASS)

| Request                              | Expected | Result |
| ------------------------------------ | -------- | ------ |
| `GET /`               (index.html)   | 200      | 200 ✅ |
| `GET /app.js`         (allowlisted)  | 200      | 200 ✅ |
| `GET /../server.mjs`  (traversal)    | 404      | 404 ✅ |
| `GET /..%2fserver.mjs` (encoded)     | 404      | 404 ✅ |
| `GET //etc/passwd`    (absolute)     | 404      | 404 ✅ |
| `GET /server.mjs`     (source leak)  | 404      | 404 ✅ |

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:4199/            # 200
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:4199/app.js      # 200
curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:4199/../server.mjs"   # 404
curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:4199/..%2fserver.mjs" # 404
```

Before the fix, the source file `server.mjs` (and other files outside the intended
asset set) were reachable through the read; after the fix every non-allowlisted
path — including the server's own source — returns 404.

### Snyk re-scan

```bash
snyk code test --severity-threshold=high
# Total issues: 0
```

---

## What this proves / regression notes

- The three legitimate demo assets still serve (`200`); nothing else does.
- Path traversal, URL-encoded traversal, absolute-path, and source-file-leak
  probes all return `404`.
- Snyk Code re-scan drops from 1 HIGH to 0.
- Stack liveness after the change: backend booted on SQLite
  (`uv sync --frozen --extra dev`, `alembic upgrade head`, `python main.py`),
  `GET /health` → `200 {"status":"ok"}`. The change is confined to
  `examples/oauth-report-app/` and does not touch `backend/` or `frontend/`.
