// SSO sign-in when BOW itself is shown inside another site's iframe.
//
// An identity provider's login page refuses to render in a frame (Microsoft
// sends `X-Frame-Options: DENY`, and so does every other major provider), so
// the usual full-page redirect leaves the frame blank. When the sign-in page
// is framed, the provider button opens a popup on the BOW origin instead:
//
//   frame   opens  /users/sign-in?sso_popup=<state>&provider=<name>
//   popup   remembers <state>, starts the provider's flow (a normal top-level
//           redirect, first-party cookies), and comes back to /users/sign-in
//           with ?login_code= (or ?error_code=)
//   popup   hands that single-use code to the frame and closes
//   frame   loads /users/sign-in?login_code=…, which signs it in as it would
//           after a full-page round trip
//
// What crosses between the two windows is the single-use login code, never
// the token, and only between windows on this origin: `postMessage` with this
// origin as the target, and a BroadcastChannel (same-origin by definition) for
// providers whose pages cut the popup's link to its opener.

export const SSO_POPUP_QUERY = 'sso_popup'
export const SSO_POPUP_PROVIDER_QUERY = 'provider'

const STATE_STORAGE = 'bow:ssoPopupState'
const CHANNEL = 'bow:sso-popup'
const MESSAGE_TYPE = 'bow:sso-popup-result'

const STATE_PATTERN = /^[A-Za-z0-9-]{16,64}$/
const PROVIDER_PATTERN = /^[A-Za-z0-9_.-]{1,64}$/
const LOGIN_CODE_PATTERN = /^[A-Za-z0-9_.~-]{8,512}$/
const ERROR_CODE_PATTERN = /^[a-z0-9_.]{1,64}$/

export type SsoPopupResult = { login_code: string } | { error_code: string }

// ── pure helpers (tests/unit/ssoPopup.mjs) ─────────────────────────────────

export function isValidPopupState(value: unknown): value is string {
  return typeof value === 'string' && STATE_PATTERN.test(value)
}

/** The page the frame opens in the popup. */
export function ssoPopupUrl(provider: string, state: string, loginHint = ''): string {
  const query = new URLSearchParams({ [SSO_POPUP_QUERY]: state, [SSO_POPUP_PROVIDER_QUERY]: provider })
  if (loginHint) query.set('login_hint', loginHint)
  return `/users/sign-in?${query.toString()}`
}

/** What the popup was asked to do, read from its own URL; null when it is not a valid ask. */
export function readPopupRequest(query: Record<string, unknown>): { state: string, provider: string } | null {
  const state = query[SSO_POPUP_QUERY]
  const provider = query[SSO_POPUP_PROVIDER_QUERY]
  if (!isValidPopupState(state)) return null
  if (typeof provider !== 'string' || !PROVIDER_PATTERN.test(provider)) return null
  return { state, provider }
}

/** What the provider round trip left in the popup's URL; null when there is nothing to hand over. */
export function resultFromQuery(query: Record<string, unknown>): SsoPopupResult | null {
  const loginCode = query.login_code
  if (typeof loginCode === 'string' && LOGIN_CODE_PATTERN.test(loginCode)) return { login_code: loginCode }
  const errorCode = query.error_code
  if (typeof errorCode === 'string' && ERROR_CODE_PATTERN.test(errorCode)) return { error_code: errorCode }
  return null
}

export function resultMessage(state: string, result: SsoPopupResult) {
  return { type: MESSAGE_TYPE, state, ...result }
}

/** A message from the popup, accepted only for the attempt that is waiting on `state`. */
export function parseResultMessage(data: any, state: string): SsoPopupResult | null {
  if (!data || typeof data !== 'object' || data.type !== MESSAGE_TYPE || data.state !== state) return null
  return resultFromQuery({ login_code: data.login_code, error_code: data.error_code })
}

/** Where the frame goes with the popup's result: the sign-in page finishes (or reports) it. */
export function resultUrl(result: SsoPopupResult, redirect: string | null = null): string {
  const query = new URLSearchParams(result as Record<string, string>)
  // A success takes its post-sign-in target from sessionStorage, as after a
  // full-page round trip; a failure stays on the sign-in page and keeps it.
  if ('error_code' in result && redirect) query.set('redirect', redirect)
  return `/users/sign-in?${query.toString()}`
}

// ── browser side ───────────────────────────────────────────────────────────

/** True when this page is inside a frame (a cross-origin parent throws: framed). */
export function isFramed(): boolean {
  if (typeof window === 'undefined') return false
  try { return window.self !== window.top } catch { return true }
}

function openerOnThisOrigin(): Window | null {
  try {
    const opener = window.opener as Window | null
    return opener && opener.location.origin === window.location.origin ? opener : null
  } catch { return null }
}

/**
 * Popup, on arrival: remember the attempt across the provider round trip.
 * Honored only when a window on this origin opened us, so a link on another
 * site cannot use the popup URL to start a sign-in nobody clicked for.
 */
export function beginPopupFlow(query: Record<string, unknown>): { provider: string } | null {
  const request = readPopupRequest(query)
  if (!request || !openerOnThisOrigin()) return null
  try { sessionStorage.setItem(STATE_STORAGE, request.state) } catch { return null }
  return { provider: request.provider }
}

/**
 * Popup, back from the provider: hand the result to the frame that opened us
 * and close. False when this window is not a sign-in popup (or has nothing to
 * hand over), and the page carries on as usual.
 */
export function deliverPopupResult(query: Record<string, unknown>): boolean {
  let state: string | null = null
  try { state = sessionStorage.getItem(STATE_STORAGE) } catch { /* no storage: not a popup flow */ }
  if (!isValidPopupState(state)) return false
  const result = resultFromQuery(query)
  if (!result) return false
  try { sessionStorage.removeItem(STATE_STORAGE) } catch { /* nothing to clear */ }
  const message = resultMessage(state, result)
  try { openerOnThisOrigin()?.postMessage(message, window.location.origin) } catch { /* opener gone */ }
  try {
    const channel = new BroadcastChannel(CHANNEL)
    channel.postMessage(message)
    channel.close()
  } catch { /* no BroadcastChannel */ }
  setTimeout(() => window.close(), 150)
  return true
}

/**
 * Frame: run the provider's sign-in in a popup. Must be called straight from
 * the click (before any await) or the browser blocks the window. Returns a
 * function that abandons the attempt, or null when the popup was blocked.
 */
export function openSsoPopup(
  provider: string,
  handlers: { onResult: (result: SsoPopupResult) => void, onClosed: () => void },
  loginHint = '',
): (() => void) | null {
  const state = crypto.randomUUID()
  const popup = window.open(ssoPopupUrl(provider, state, loginHint), 'bow-sso-signin', 'popup,width=520,height=680')
  if (!popup) return null

  let channel: BroadcastChannel | null = null
  let closedTimer: ReturnType<typeof setInterval> | null = null
  let done = false

  const stopWatchingClose = () => {
    if (closedTimer) clearInterval(closedTimer)
    closedTimer = null
  }
  const cleanup = () => {
    done = true
    stopWatchingClose()
    window.removeEventListener('message', onMessage)
    try { channel?.close() } catch { /* already closed */ }
    channel = null
  }
  const accept = (data: any) => {
    if (done) return
    const result = parseResultMessage(data, state)
    if (!result) return
    cleanup()
    try { popup.close() } catch { /* already closed */ }
    handlers.onResult(result)
  }
  function onMessage(event: MessageEvent) {
    if (event.origin !== window.location.origin || event.source !== popup) return
    accept(event.data)
  }

  window.addEventListener('message', onMessage)
  try {
    channel = new BroadcastChannel(CHANNEL)
    channel.onmessage = (event) => accept(event.data)
  } catch { /* no BroadcastChannel: the opener message is enough */ }

  // A closed popup frees the button, but the attempt keeps listening: a
  // provider page that isolates itself (COOP) makes the popup read as closed
  // while it is still signing the user in, and its result then arrives on the
  // channel. The listeners go when a result lands or the caller abandons.
  closedTimer = setInterval(() => {
    let closed = true
    try { closed = popup.closed } catch { /* unreadable: treat as closed */ }
    if (!closed || done) return
    stopWatchingClose()
    handlers.onClosed()
  }, 500)

  return cleanup
}
