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

/** The config to save, without an auth type the form only guessed for a row that had none. */
export function withoutGuessedAuthType(config: Record<string, any>, guessed: string | null): Record<string, any> {
  if (!guessed || config.auth_type !== guessed) return config
  const { auth_type: _guessed, ...rest } = config
  return rest
}

/** Whether a re-entry notice has served its purpose. */
export function reentryNoticeResolved(state: {
  missingFields: string[]
  credentials: Record<string, unknown>
  testPassed: boolean
  refusedForHostChange: boolean
  hostStillChanged: boolean
}): boolean {
  if (state.testPassed) return true
  if (state.refusedForHostChange && !state.hostStillChanged) return true
  return state.missingFields.length > 0
    && state.missingFields.every(field => String(state.credentials?.[field] ?? '').trim() !== '')
}

/** True for the server's refusal asking for the secret to be re-entered. */
export function needsCredentialReentry(err: any): boolean {
  return (err?.data?.error_code ?? err?.response?._data?.error_code) === CREDENTIALS_REQUIRED_CODE
}
