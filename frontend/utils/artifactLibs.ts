/**
 * Parent-side loader for the artifact iframe's vendored libraries.
 *
 * The artifact iframe is sandboxed WITHOUT allow-same-origin, so it runs in an
 * opaque origin — and Chrome never serves an opaque-origin frame's requests
 * from the HTTP cache. Loaded by <script src>, every view re-downloaded
 * ~1.3MB of React/ECharts/Tailwind/… and fetched the files one after another.
 *
 * Instead the host page (same origin, normal HTTP cache) fetches the files in
 * parallel as soon as it mounts and hands their text to buildArtifactIframeHtml,
 * which runs each one as an inline script in the same order as before. With
 * the content-hashed URLs from libUrl() and the immutable Cache-Control the
 * backend serves them with, a repeat visit reads them from disk cache.
 *
 * Any lib that fails or times out is simply left out of the returned map and
 * the builder falls back to a <script src> tag for it — today's behavior.
 */
import { reactive } from 'vue';

const FETCH_TIMEOUT_MS = 8000;

// url -> source text, or false when the fetch failed (fall back to src).
const store = reactive<Record<string, string | false>>({});
const settled = new Map<string, Promise<void>>();

/** Start fetching `urls` (idempotent). Safe to call from setup or a computed. */
export function ensureArtifactLibs(urls: string[]): void {
  if (typeof window === 'undefined' || typeof fetch !== 'function') {
    for (const url of urls) if (!(url in store)) store[url] = false;
    return;
  }
  for (const url of urls) {
    if (settled.has(url)) continue;
    const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    const timer = setTimeout(() => ctrl?.abort(), FETCH_TIMEOUT_MS);
    // Low priority: the page's own data requests (which the artifact also
    // waits for) must not queue behind ~1MB of libraries.
    settled.set(url, fetch(url, { credentials: 'same-origin', signal: ctrl?.signal, priority: 'low' } as RequestInit)
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(String(r.status)))))
      .then((text) => { store[url] = text; })
      .catch(() => { store[url] = false; })
      .finally(() => clearTimeout(timer)));
  }
}

/** Resolves once every fetch for `urls` has settled (never rejects). */
export function whenArtifactLibsSettled(urls: string[]): Promise<void> {
  ensureArtifactLibs(urls);
  return Promise.all(urls.map((u) => settled.get(u) || Promise.resolve())).then(() => undefined);
}

/**
 * Sources for `urls` once every fetch has settled (reactive — read it inside a
 * computed), or null while any is still in flight. Failed urls are omitted.
 */
export function artifactLibSources(urls: string[]): Record<string, string> | null {
  ensureArtifactLibs(urls);
  const out: Record<string, string> = {};
  for (const url of urls) {
    const v = store[url];
    if (v === undefined) return null;
    if (v !== false) out[url] = v;
  }
  return out;
}
