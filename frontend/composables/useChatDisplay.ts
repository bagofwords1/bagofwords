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
 * Five steps, not a free slider: a slider invites 1.07x, which lands chat
 * text on a half-pixel and makes every line box in the transcript reflow
 * differently. As fractions of the 16px root these are 14 / 15 / 16 / 18 / 20px.
 *
 * The default is the second step, 15px — see the note in
 * assets/css/tokens.css. 14px (the old default) stays as the first step,
 * "Small", for readers who prefer the denser transcript. A 14px saved by the
 * old menu was its "Default" and moves to the new default (see SCALE_KEY).
 */
export const CHAT_TEXT_SCALES = [0.875, 0.9375, 1, 1.125, 1.25] as const
export type ChatTextScale = (typeof CHAT_TEXT_SCALES)[number]

export const DEFAULT_CHAT_WIDTH: ChatWidth = 'standard'
export const DEFAULT_CHAT_TEXT_SCALE: ChatTextScale = 0.9375

const WIDTH_KEY = 'bow.chat.width'
/*
 * v2 since the default moved from 14px to 15px. In the old menu 14px *was*
 * the step labelled "Default" and nothing was smaller, so a stored 14px only
 * ever meant "the default" — read through literally it would pin everyone
 * who ever clicked "Default" to what is now "Small". The legacy key is read
 * once: its larger steps carry over as real choices, 14px falls back to the
 * new default, and it is removed either way.
 */
const SCALE_KEY = 'bow.chat.textScale.v2'
const LEGACY_SCALE_KEY = 'bow.chat.textScale'
const LEGACY_DEFAULT_SCALE = 0.875

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

/* What a legacy (v1) stored scale means today: a larger step is a choice the
   reader made and carries over; the old default (14px) or anything unknown
   means "no choice", so the current default applies. */
export function migrateLegacyTextScale(raw: string | null): ChatTextScale | null {
  if (raw === null) return null
  const value = Number(raw)
  if (value === LEGACY_DEFAULT_SCALE) return null
  return isChatTextScale(value) ? value : null
}

function readTextScale(): ChatTextScale | null {
  const current = readStored(SCALE_KEY, isChatTextScale)
  if (current !== null) return current
  if (typeof localStorage === 'undefined') return null
  try {
    const legacy = localStorage.getItem(LEGACY_SCALE_KEY)
    if (legacy === null) return null
    localStorage.removeItem(LEGACY_SCALE_KEY)
    const migrated = migrateLegacyTextScale(legacy)
    if (migrated !== null) localStorage.setItem(SCALE_KEY, String(migrated))
    return migrated
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
    textScale.value = readTextScale() ?? DEFAULT_CHAT_TEXT_SCALE
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
