/**
 * Short-lived, file-scoped URLs for <iframe> / <img> sources.
 *
 * `/api/files/{id}/content` serves `Content-Disposition: attachment`, so an
 * iframe pointed at it downloads the file instead of rendering it. `/embed`
 * serves the same bytes `inline` and authorizes with a capability token in the
 * query string — which is also what makes `#page=N` work: the browser's native
 * PDF viewer reads that fragment, and a `blob:` URL does not pass it through
 * reliably across browsers.
 *
 * Tokens are minted per render and live an hour (backend core/file_tokens.py),
 * so they are cached here and re-minted a few minutes BEFORE expiry rather than
 * on failure — a chat tab left open overnight would otherwise show a dead frame
 * with no way to know it needs refreshing.
 */
const TOKEN_TTL_MS = 60 * 60 * 1000
const REFRESH_MARGIN_MS = 5 * 60 * 1000

const cache = new Map<string, { url: string; mintedAt: number }>()

export function useFileEmbedUrl() {
  async function getEmbedUrl(fileId: string): Promise<string> {
    if (!fileId) return ''

    const hit = cache.get(fileId)
    if (hit && Date.now() - hit.mintedAt < TOKEN_TTL_MS - REFRESH_MARGIN_MS) {
      return hit.url
    }

    try {
      const { data } = await useMyFetch<any>(`/api/files/${fileId}/embed_token`)
      const url = (data.value as any)?.url || ''
      if (!url) return ''
      cache.set(fileId, { url, mintedAt: Date.now() })
      return url
    } catch (e) {
      console.error('[useFileEmbedUrl] failed to mint embed token for', fileId, e)
      return ''
    }
  }

  /** Drop a cached token — for a retry after the server rejected one. */
  function invalidate(fileId: string) {
    cache.delete(fileId)
  }

  return { getEmbedUrl, invalidate }
}
