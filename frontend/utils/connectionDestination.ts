// Saved connection forms: spotting a destination change on the client so the
// credential fields can be unlocked, and recognizing the server's refusal.

const DESTINATION_FIELD = /(^|_)(host|hostname|port|url|uri|endpoint|account|domain)($|_)/i

export const CREDENTIALS_REQUIRED_CODE = 'connection.credentials_required'

function normalized(value: unknown): string {
  return String(value ?? '').trim().toLowerCase().replace(/\/+$/, '')
}

/** Config keys naming the destination whose value differs from what was saved. */
export function changedDestinationFields(saved: Record<string, unknown>, current: Record<string, unknown>): string[] {
  const keys = new Set([...Object.keys(saved || {}), ...Object.keys(current || {})])
  return [...keys].filter(key => DESTINATION_FIELD.test(key) && normalized(saved?.[key]) !== normalized(current?.[key]))
}

/** True for the server's refusal asking for the secret to be re-entered. */
export function needsCredentialReentry(err: any): boolean {
  return (err?.data?.error_code ?? err?.response?._data?.error_code) === CREDENTIALS_REQUIRED_CODE
}
