// SSO sign-in when BOW itself is shown inside another site's iframe.
//
// An identity provider's login page refuses to render in a frame (Microsoft
// sends `X-Frame-Options: DENY`, and so does every other major provider), so
// the usual full-page redirect leaves the frame blank. When the sign-in page
// is framed, the provider button opens a popup on the BOW origin instead:
//
//   frame   makes a secret verifier and opens
//           /users/sign-in?sso_popup=<state>&challenge=<sha256(verifier)>&provider=<name>
//   popup   remembers state + challenge, starts the provider's flow (a normal
//           top-level redirect, first-party cookies), and comes back to
//           /users/sign-in with ?login_code= (or ?error_code=)
//   popup   spends its login code against the challenge
//           (POST /api/auth/exchange/handoff)
//   frame   sees the handoff is ready (GET /api/auth/exchange/handoff/<challenge>),
//           and loads /users/sign-in?login_code=<verifier>, which signs it in
//           as it would after a full-page round trip
//   popup   closes once the server says the frame took the session
//
// The result goes through the server because the two windows may share no
// browser channel at all. A provider page that isolates itself (COOP) cuts
// the popup's link to its opener, and when the embedding site is a different
// site from BOW, the top-level popup and the framed page are in different
// storage partitions, so a BroadcastChannel does not connect them either.
// The server does. `postMessage` and the channel stay as hints that make the
// frame look sooner, and as the path for a provider *failure*, which has no
// session to hand over.
//
// The token never crosses between the windows. The frame's verifier is the
// only thing that redeems the handoff, and it never leaves the frame.

export const SSO_POPUP_QUERY = 'sso_popup'
export const SSO_POPUP_CHALLENGE_QUERY = 'challenge'
export const SSO_POPUP_PROVIDER_QUERY = 'provider'

const FLOW_STORAGE = 'bow:ssoPopupFlow'
const CHANNEL = 'bow:sso-popup'
const MESSAGE_TYPE = 'bow:sso-popup-result'
const ACK_TYPE = 'bow:sso-popup-ack'
const HANDOFF_API = '/api/auth/exchange/handoff'

const STATE_PATTERN = /^[A-Za-z0-9-]{16,64}$/
const CHALLENGE_PATTERN = /^[a-f0-9]{64}$/
const PROVIDER_PATTERN = /^[A-Za-z0-9_.-]{1,64}$/
const LOGIN_CODE_PATTERN = /^[A-Za-z0-9_.~-]{8,512}$/
const ERROR_CODE_PATTERN = /^[a-z0-9_.]{1,64}$/

export type SsoPopupResult = { login_code: string } | { error_code: string }
type PopupFlow = { state: string, challenge: string }
/** What the popup hands the frame: a session waiting on the server, or why there is none. */
type PopupSignal = { ready: true } | { error_code: string }

// ── pure helpers (tests/unit/ssoPopup.mjs) ─────────────────────────────────

export function isValidPopupState(value: unknown): value is string {
  return typeof value === 'string' && STATE_PATTERN.test(value)
}

export function isValidChallenge(value: unknown): value is string {
  return typeof value === 'string' && CHALLENGE_PATTERN.test(value)
}

const hex = (bytes: Uint8Array) => Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')

/**
 * Random hex. `crypto.getRandomValues` and not `crypto.randomUUID`: the latter
 * does not exist outside a secure context, and a self-hosted BOW on plain HTTP
 * is not one.
 */
export function randomHex(byteLength: number): string {
  return hex(crypto.getRandomValues(new Uint8Array(byteLength)))
}

const SHA256_K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
])

/**
 * SHA-256 of a string, as lowercase hex. Written out rather than taken from
 * `crypto.subtle` for two reasons: `subtle` is missing outside a secure
 * context, and it is asynchronous, while the popup must open in the same
 * task as the click or the browser blocks it.
 */
export function sha256Hex(text: string): string {
  const input = new TextEncoder().encode(text)
  const padded = new Uint8Array((((input.length + 8) >> 6) + 1) << 6)
  padded.set(input)
  padded[input.length] = 0x80
  const view = new DataView(padded.buffer)
  view.setUint32(padded.length - 8, Math.floor(input.length / 0x20000000))
  view.setUint32(padded.length - 4, (input.length << 3) >>> 0)
  const h = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19])
  const w = new Uint32Array(64)
  const rotr = (x: number, n: number) => (x >>> n) | (x << (32 - n))
  for (let offset = 0; offset < padded.length; offset += 64) {
    for (let i = 0; i < 16; i++) w[i] = view.getUint32(offset + i * 4)
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3)
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10)
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0
    }
    let [a, b, c, d, e, f, g, k] = h
    for (let i = 0; i < 64; i++) {
      const t1 = (k + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g)) + SHA256_K[i] + w[i]) | 0
      const t2 = ((rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) | 0
      k = g; g = f; f = e; e = (d + t1) | 0
      d = c; c = b; b = a; a = (t1 + t2) | 0
    }
    h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += k
  }
  return Array.from(h, (word) => word.toString(16).padStart(8, '0')).join('')
}

/**
 * The page the frame opens in the popup. `redirect` is the frame's own
 * (already same-origin-checked) target: the popup's sign-in reads things off
 * it, such as the host application of an embed sign-in window, which binds an
 * account the provider creates to that application's role.
 */
export function ssoPopupUrl(provider: string, state: string, challenge: string, loginHint = '', redirect: string | null = null): string {
  const query = new URLSearchParams({
    [SSO_POPUP_QUERY]: state,
    [SSO_POPUP_CHALLENGE_QUERY]: challenge,
    [SSO_POPUP_PROVIDER_QUERY]: provider,
  })
  if (loginHint) query.set('login_hint', loginHint)
  if (redirect) query.set('redirect', redirect)
  return `/users/sign-in?${query.toString()}`
}

/** What the popup was asked to do, read from its own URL; null when it is not a valid ask. */
export function readPopupRequest(query: Record<string, unknown>): (PopupFlow & { provider: string }) | null {
  const state = query[SSO_POPUP_QUERY]
  const challenge = query[SSO_POPUP_CHALLENGE_QUERY]
  const provider = query[SSO_POPUP_PROVIDER_QUERY]
  if (!isValidPopupState(state) || !isValidChallenge(challenge)) return null
  if (typeof provider !== 'string' || !PROVIDER_PATTERN.test(provider)) return null
  return { state, challenge, provider }
}

/** What the provider round trip left in the popup's URL; null when there is nothing to hand over. */
export function resultFromQuery(query: Record<string, unknown>): SsoPopupResult | null {
  const loginCode = query.login_code
  if (typeof loginCode === 'string' && LOGIN_CODE_PATTERN.test(loginCode)) return { login_code: loginCode }
  const errorCode = query.error_code
  if (typeof errorCode === 'string' && ERROR_CODE_PATTERN.test(errorCode)) return { error_code: errorCode }
  return null
}

export function signalMessage(state: string, signal: PopupSignal) {
  return { type: MESSAGE_TYPE, state, ...signal }
}

/** A message from the popup, accepted only for the attempt that is waiting on `state`. */
export function parseSignalMessage(data: any, state: string): PopupSignal | null {
  if (!data || typeof data !== 'object' || data.type !== MESSAGE_TYPE || data.state !== state) return null
  if (data.ready === true) return { ready: true }
  return typeof data.error_code === 'string' && ERROR_CODE_PATTERN.test(data.error_code)
    ? { error_code: data.error_code }
    : null
}

export function ackMessage(state: string) {
  return { type: ACK_TYPE, state }
}

export function isAckMessage(data: any, state: string): boolean {
  return !!data && typeof data === 'object' && data.type === ACK_TYPE && data.state === state
}

/**
 * What the popup concludes from two successive handoff statuses. The server
 * sweeps consumed rows, so `ready` followed by `pending` also means the frame
 * took it. That reading holds only while the handoff is still within its
 * lifetime (expired rows are swept too), which is why the popup stops waiting
 * before then (HANDOFF_WAIT_MS) and why the server reports an unswept,
 * untaken one as `expired`.
 */
export function frameTookHandoff(previous: string | null, current: string | null): boolean {
  return current === 'consumed' || (previous === 'ready' && current === 'pending')
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

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

async function handoffStatus(challenge: string): Promise<string | null> {
  try {
    const res = await fetch(`${HANDOFF_API}/${challenge}`, { cache: 'no-store', credentials: 'omit' })
    if (!res.ok) return null
    const status = (await res.json())?.status
    return typeof status === 'string' ? status : null
  } catch { return null }
}

function readPopupFlow(): PopupFlow | null {
  try {
    const flow = JSON.parse(sessionStorage.getItem(FLOW_STORAGE) || 'null')
    return flow && isValidPopupState(flow.state) && isValidChallenge(flow.challenge)
      ? { state: flow.state, challenge: flow.challenge }
      : null
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
  try {
    sessionStorage.setItem(FLOW_STORAGE, JSON.stringify({ state: request.state, challenge: request.challenge }))
  } catch { return null }
  return { provider: request.provider }
}

// Best effort, both ways: the opener (gone after a COOP provider page) and the
// channel (not shared across storage partitions). Neither is relied on.
function hintFrame(message: Record<string, unknown>) {
  try { openerOnThisOrigin()?.postMessage(message, window.location.origin) } catch { /* opener gone */ }
  try {
    const channel = new BroadcastChannel(CHANNEL)
    channel.postMessage(message)
    channel.close()
  } catch { /* no BroadcastChannel */ }
}

// How long the popup waits for the frame, counted from the handoff. It must
// end before the handed-off session's 60 s server lifetime: past that, an
// untaken session is swept and its row vanishes just as a taken one's does.
const HANDOFF_WAIT_MS = 50_000
const ERROR_ACK_WAIT_MS = 4_000

/** Popup: a provider failure has no session to hand over, so it needs the frame's own acknowledgement. */
function deliverError(flow: PopupFlow, errorCode: string): Promise<boolean> {
  return new Promise((resolve) => {
    let channel: BroadcastChannel | null = null
    const finish = (acknowledged: boolean) => {
      clearTimeout(timer)
      window.removeEventListener('message', onMessage)
      try { channel?.close() } catch { /* already closed */ }
      resolve(acknowledged)
    }
    const onMessage = (event: MessageEvent) => {
      if (event.origin === window.location.origin && isAckMessage(event.data, flow.state)) finish(true)
    }
    const timer = setTimeout(() => finish(false), ERROR_ACK_WAIT_MS)
    window.addEventListener('message', onMessage)
    try {
      channel = new BroadcastChannel(CHANNEL)
      channel.onmessage = (event) => { if (isAckMessage(event.data, flow.state)) finish(true) }
    } catch { /* no BroadcastChannel */ }
    hintFrame(signalMessage(flow.state, { error_code: errorCode }))
  })
}

/**
 * Popup, back from the provider: hand the result to the frame that opened us,
 * and close once the frame has it.
 *
 *   'not-popup'    this window is not a sign-in popup (or the URL carries no
 *                  result): the page carries on as usual
 *   'closing'      the frame acknowledged; the window is closing
 *   'undelivered'  nothing acknowledged. The window stays open so the caller
 *                  can say so; closing it would leave the user looking at a
 *                  sign-in page that silently did nothing
 */
export async function deliverPopupResult(
  query: Record<string, unknown>,
): Promise<'not-popup' | 'closing' | 'undelivered'> {
  const flow = readPopupFlow()
  const result = flow && resultFromQuery(query)
  if (!flow || !result) return 'not-popup'
  try { sessionStorage.removeItem(FLOW_STORAGE) } catch { /* nothing to clear */ }

  let delivered = false
  if ('error_code' in result) {
    delivered = await deliverError(flow, result.error_code)
  } else {
    let handedOff = false
    try {
      const res = await fetch(HANDOFF_API, {
        method: 'POST',
        credentials: 'omit',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ login_code: result.login_code, code_challenge: flow.challenge }),
      })
      handedOff = res.ok
    } catch { /* network: undelivered */ }
    if (handedOff) {
      hintFrame(signalMessage(flow.state, { ready: true }))
      const deadline = Date.now() + HANDOFF_WAIT_MS
      let previous: string | null = null
      while (!delivered && Date.now() < deadline) {
        const status = await handoffStatus(flow.challenge)
        if (status === 'expired') break
        delivered = frameTookHandoff(previous, status)
        if (status) previous = status
        if (!delivered) await sleep(700)
      }
    }
  }
  if (!delivered) return 'undelivered'
  window.close()
  return 'closing'
}

/**
 * Frame: run the provider's sign-in in a popup. Must be called straight from
 * the click (nothing awaited before it) or the browser blocks the window.
 * Returns a function that abandons the attempt, or null when the popup was
 * blocked.
 */
export function openSsoPopup(
  provider: string,
  handlers: { onResult: (result: SsoPopupResult) => void, onClosed: () => void },
  loginHint = '',
  redirect: string | null = null,
): (() => void) | null {
  const state = randomHex(16)
  // The verifier is what redeems the handoff; only its hash leaves this frame.
  const verifier = randomHex(32)
  const challenge = sha256Hex(verifier)
  const popup = window.open(ssoPopupUrl(provider, state, challenge, loginHint, redirect), 'bow-sso-signin', 'popup,width=520,height=680')
  if (!popup) return null

  let channel: BroadcastChannel | null = null
  let closedTimer: ReturnType<typeof setInterval> | null = null
  let pollTimer: ReturnType<typeof setTimeout> | null = null
  let done = false
  const startedAt = Date.now()

  const stopWatchingClose = () => {
    if (closedTimer) clearInterval(closedTimer)
    closedTimer = null
  }
  const cleanup = () => {
    done = true
    stopWatchingClose()
    if (pollTimer) clearTimeout(pollTimer)
    pollTimer = null
    window.removeEventListener('message', onMessage)
    try { channel?.close() } catch { /* already closed */ }
    channel = null
  }
  // The handoff is on the server: the verifier is now a login code, and the
  // sign-in page exchanges it. The popup closes itself when it sees that.
  const takeHandoff = () => {
    if (done) return
    cleanup()
    handlers.onResult({ login_code: verifier })
  }
  const checkHandoff = async () => {
    if (done) return
    if (await handoffStatus(challenge) === 'ready') takeHandoff()
  }
  // Tell the popup its failure was received, on every path that might reach
  // it, and before the channel is closed by cleanup.
  const acknowledge = () => {
    const message = ackMessage(state)
    try { popup.postMessage(message, window.location.origin) } catch { /* popup gone */ }
    try { channel?.postMessage(message) } catch { /* channel closed */ }
  }
  const accept = (data: any) => {
    if (done) return
    const signal = parseSignalMessage(data, state)
    if (!signal) return
    if ('ready' in signal) { checkHandoff(); return }
    acknowledge()
    cleanup()
    handlers.onResult({ error_code: signal.error_code })
  }
  function onMessage(event: MessageEvent) {
    if (event.origin !== window.location.origin || event.source !== popup) return
    accept(event.data)
  }

  window.addEventListener('message', onMessage)
  try {
    channel = new BroadcastChannel(CHANNEL)
    channel.onmessage = (event) => accept(event.data)
  } catch { /* no BroadcastChannel: the server poll is enough */ }

  // The path that always works: ask the server. Frequent while a sign-in is
  // plausibly in progress, then slower; given up after 15 minutes.
  const poll = async () => {
    pollTimer = null
    await checkHandoff()
    const elapsed = Date.now() - startedAt
    if (done || elapsed > 15 * 60_000) return
    pollTimer = setTimeout(poll, elapsed < 3 * 60_000 ? 1500 : 4000)
  }
  pollTimer = setTimeout(poll, 1500)

  // A closed popup frees the button, but the attempt keeps listening: a
  // provider page that isolates itself (COOP) makes the popup read as closed
  // while it is still signing the user in. The attempt ends when a result
  // lands or the caller abandons it.
  closedTimer = setInterval(() => {
    let closed = true
    try { closed = popup.closed } catch { /* unreadable: treat as closed */ }
    if (!closed || done) return
    stopWatchingClose()
    handlers.onClosed()
  }, 500)

  return cleanup
}
