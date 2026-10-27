/**
 * Resolve a localized error message from a fetch error.
 *
 * The backend's `AppError` handler returns JSON of the shape:
 *   { detail, error_code, params, status_code }
 *
 * When `error_code` is present and the current i18n catalog has a matching
 * `errors.<error_code>` key, we return the localized message (with params
 * interpolated). Otherwise we fall back to the server-provided `detail` /
 * `message`, and finally to a generic localized string.
 *
 * Usage:
 *   const { getErrorMessage } = useErrorMessage()
 *   try { await doThing() } catch (e) {
 *     toast.add({ description: getErrorMessage(e, t('errors.generic')) })
 *   }
 */
/**
 * The catalog keys an error code may live under, in lookup order. Most codes
 * hold a dot ("artifact.not_found") and the catalog stores them as flat keys
 * under `errors`, but vue-i18n reads a dotted path as nesting
 * (errors → artifact → not_found), so the flat key needs bracket form.
 */
export function errorCatalogKeys(code: string): string[] {
  return [`errors.${code}`, `errors['${code}']`]
}

/**
 * The localized message for an AppError payload, or null when the catalog
 * has no entry for its code. The catalog wins over the server's `detail`
 * whenever it has one: `detail` is the server's English (backend/AGENTS.md:
 * "the frontend localizes via the error_code").
 */
export function catalogMessage(
  payload: { error_code?: string; params?: Record<string, unknown> },
  t: (key: string, params: Record<string, unknown>) => string,
  te: (key: string) => boolean,
): string | null {
  const code = payload.error_code
  if (!code) return null
  const key = errorCatalogKeys(code).find((k) => te(k))
  return key ? t(key, (payload.params ?? {}) as Record<string, unknown>) : null
}

export function useErrorMessage() {
  const { t, te } = useI18n()

  function extractPayload(err: unknown): {
    error_code?: string
    detail?: string | Array<{ loc?: unknown[]; msg?: string }>
    message?: string
    params?: Record<string, unknown>
  } {
    if (!err || typeof err !== 'object') return {}
    const anyErr = err as any
    // Nuxt/Ofetch error wraps the body on .data; direct responses may have .response._data
    const data =
      anyErr?.data ??
      anyErr?.response?._data ??
      anyErr?.value?.data ??
      {}
    if (typeof data !== 'object' || data === null) return {}
    return data as any
  }

  function getErrorMessage(err: unknown, fallback?: string): string {
    const payload = extractPayload(err)
    const localized = catalogMessage(payload, t, te)
    if (localized) return localized
    // Fall back to server-provided English string, then caller fallback, then generic.
    if (typeof payload.detail === 'string' && payload.detail) return payload.detail
    // FastAPI validation responses are arrays, not localized AppError envelopes.
    // Include only the field path and message, never echoed input values.
    if (Array.isArray(payload.detail)) {
      const messages = payload.detail.slice(0, 5).filter(item => item && typeof item.msg === 'string')
        .map(item => `${(item.loc || []).filter(part => part !== 'body').join('.')}: ${item.msg}`)
      if (messages.length) return messages.join('; ')
    }
    if (typeof payload.message === 'string' && payload.message) return payload.message
    if (fallback) return fallback
    return t('errors.generic')
  }

  function getErrorCode(err: unknown): string | undefined {
    return extractPayload(err).error_code
  }

  return { getErrorMessage, getErrorCode }
}
