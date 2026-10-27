/*
 * Reader preferences for the conversation surface: how wide it runs, and how
 * large its text is.
 *
 * These are *personal*, not organizational, so they live in localStorage per
 * browser rather than in `OrganizationSettings` — the same split the locale
 * uses (`bow.locale` is the personal override, the org value is the
 * fallback). Nobody should have to ask an admin to make text bigger.
 *
 * Both values resolve through CSS variables (`--bow-measure-chat`,
 * `--bow-chat-text-scale`) declared in `assets/css/tokens.css`, so applying
 * a preference is one write to <html> and no component re-renders or has to
 * know which mode it is in.
 */

export type ChatWidth = 'standard' | 'wide'

/*
 * Four steps, not a free slider: a slider invites 1.07x, which lands chat
 * text on a half-pixel and makes every line box in the transcript reflow
 * differently. These multiply the 16px base to 14 / 16 / 18 / 20px.
 */
export const CHAT_TEXT_SCALES = [0.875, 1, 1.125, 1.25] as const
export type ChatTextScale = (typeof CHAT_TEXT_SCALES)[number]

export const DEFAULT_CHAT_WIDTH: ChatWidth = 'standard'
export const DEFAULT_CHAT_TEXT_SCALE: ChatTextScale = 1

const WIDTH_KEY = 'bow.chat.width'
const SCALE_KEY = 'bow.chat.textScale'

function isChatWidth(value: unknown): value is ChatWidth {
  return value === 'standard' || value === 'wide'
}

function isChatTextScale(value: unknown): value is ChatTextScale {
  return CHAT_TEXT_SCALES.includes(value as ChatTextScale)
}

/*
 * Every localStorage access is guarded: reads throw in Safari's private mode
 * and in embedded contexts (the Excel add-in runs in a webview where storage
 * can be denied outright), and a thrown preference read must not take the
 * report page down with it. A failed read means "use the default".
 */
function readStored<T>(key: string, guard: (v: unknown) => v is T): T | null {
  if (typeof localStorage === 'undefined') return null
  try {
    const raw = localStorage.getItem(key)
    if (raw === null) return null
    const parsed: unknown = key === SCALE_KEY ? Number(raw) : raw
    return guard(parsed) ? parsed : null
  } catch {
    return null
  }
}

function writeStored(key: string, value: string) {
  if (typeof localStorage === 'undefined') return
  try {
    localStorage.setItem(key, value)
  } catch {
    /* Storage denied or full — the preference still applies for this
       session, it just will not survive a reload. */
  }
}

export function useChatDisplay() {
  const width = useState<ChatWidth>('bow-chat-width', () => DEFAULT_CHAT_WIDTH)
  const textScale = useState<ChatTextScale>('bow-chat-text-scale', () => DEFAULT_CHAT_TEXT_SCALE)

  function apply() {
    if (typeof document === 'undefined') return
    const root = document.documentElement
    /* Standard is the absence of the attribute, so the default costs nothing
       and `:root[data-chat-width='wide']` stays the only override. */
    if (width.value === 'wide') root.setAttribute('data-chat-width', 'wide')
    else root.removeAttribute('data-chat-width')
    root.style.setProperty('--bow-chat-text-scale', String(textScale.value))
  }

  /* Called once from `plugins/chat-display.client.ts`. */
  function hydrate() {
    width.value = readStored(WIDTH_KEY, isChatWidth) ?? DEFAULT_CHAT_WIDTH
    textScale.value = readStored(SCALE_KEY, isChatTextScale) ?? DEFAULT_CHAT_TEXT_SCALE
    apply()
  }

  function setWidth(next: ChatWidth) {
    width.value = next
    writeStored(WIDTH_KEY, next)
    apply()
  }

  function setTextScale(next: ChatTextScale) {
    textScale.value = next
    writeStored(SCALE_KEY, String(next))
    apply()
  }

  /* The rendered size, for the "16px" readout in the picker. Derived rather
     than stored so it cannot disagree with the token. */
  const textSizePx = computed(() => Math.round(16 * textScale.value))

  return {
    width,
    textScale,
    textSizePx,
    scales: CHAT_TEXT_SCALES,
    setWidth,
    setTextScale,
    hydrate,
    apply,
  }
}
