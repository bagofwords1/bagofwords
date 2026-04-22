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
export function useErrorMessage() {
  const { t, te } = useI18n()

  function extractPayload(err: unknown): {
    error_code?: string
    detail?: string
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
    const code = payload.error_code
    if (code) {
      const key = `errors.${code}`
      if (te(key)) {
        return t(key, (payload.params ?? {}) as Record<string, unknown>)
      }
    }
    // Fall back to server-provided English string, then caller fallback, then generic.
    if (typeof payload.detail === 'string' && payload.detail) return payload.detail
    if (typeof payload.message === 'string' && payload.message) return payload.message
    if (fallback) return fallback
    return t('errors.generic')
  }

  function getErrorCode(err: unknown): string | undefined {
    return extractPayload(err).error_code
  }

  return { getErrorMessage, getErrorCode }
}
