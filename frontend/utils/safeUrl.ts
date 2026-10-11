// Guards for URLs the app hands to window.location (OAuth redirects, provider
// sign-in pages). Mirrors backend/app/core/url_safety.py: the server already
// rejects these, this is the last line in case a stale row or a compromised
// response slips through. A javascript: target would run in our origin.

const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]'])
// Whitespace, C0/C1 controls, zero-width characters. Browsers strip tabs and
// newlines inside a scheme, so "java\tscript:" would otherwise execute.
const UNSAFE_CHARS = /[\s\u0000-\u0020\u007f-\u009f\u200b-\u200d\u2060\ufeff]/
const SCHEME_RE = /^([A-Za-z][A-Za-z0-9+.-]*):/

function schemeOf(url: unknown): string | null {
  if (typeof url !== 'string' || !url || UNSAFE_CHARS.test(url)) return null
  const m = SCHEME_RE.exec(url)
  return m ? m[1].toLowerCase() : null
}

function hasHttpHost(url: string): boolean {
  try {
    return !!new URL(url).hostname && /^https?:\/\//i.test(url)
  } catch {
    return false
  }
}

/** Absolute http(s) URL with a host — for identity-provider sign-in pages. */
export function isSafeHttpUrl(url: unknown): url is string {
  const scheme = schemeOf(url)
  return (scheme === 'http' || scheme === 'https') && hasHttpHost(url as string)
}

/** OAuth client redirect target: https, or http on a loopback host only. */
export function isSafeRedirectUri(url: unknown): url is string {
  const scheme = schemeOf(url)
  if (scheme === 'https') return hasHttpHost(url as string)
  if (scheme !== 'http' || !hasHttpHost(url as string)) return false
  return LOOPBACK_HOSTS.has(new URL(url as string).hostname.toLowerCase())
}
