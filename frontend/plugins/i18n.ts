import { createI18n } from 'vue-i18n'
// Only English is bundled eagerly: it is the fallback locale, so it has to be
// present before the first render. The other nine catalogues are ~125 KB of
// JSON each and used to be static imports, which put all ten (≈1.25 MB, ~68%
// of the entry chunk) in front of every user regardless of their language.
// They are now separate chunks, fetched on demand by `loadLocaleMessages`.
import en from '../../locales/en.json'

const RTL_LOCALES = new Set(['he', 'ar', 'fa', 'ur'])

// Keep the keys in sync with the files in /locales. Vite turns each of these
// arrow functions into its own async chunk.
const LOCALE_LOADERS: Record<string, () => Promise<any>> = {
  es: () => import('../../locales/es.json'),
  he: () => import('../../locales/he.json'),
  fr: () => import('../../locales/fr.json'),
  sv: () => import('../../locales/sv.json'),
  ar: () => import('../../locales/ar.json'),
  ru: () => import('../../locales/ru.json'),
  de: () => import('../../locales/de.json'),
  pt: () => import('../../locales/pt.json'),
  it: () => import('../../locales/it.json'),
}

const SUPPORTED_LOCALES = ['en', ...Object.keys(LOCALE_LOADERS)]

const DATETIME_FORMATS = {
  short: { year: 'numeric', month: 'short', day: 'numeric' } as const,
  long: { year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit' } as const,
}

const NUMBER_FORMATS = {
  decimal: { style: 'decimal' } as const,
  integer: { style: 'decimal', maximumFractionDigits: 0 } as const,
  percent: { style: 'percent', maximumFractionDigits: 2 } as const,
  currencyUSD: { style: 'currency', currency: 'USD' } as const,
}

// The format tables are shared by every locale and cost nothing to keep eager,
// unlike the message catalogues.
const perLocale = <T,>(value: T) =>
  Object.fromEntries(SUPPORTED_LOCALES.map(code => [code, value])) as Record<string, T>

function isLocale(x: unknown): x is string {
  return typeof x === 'string' && SUPPORTED_LOCALES.includes(x)
}

function applyDocumentLocale(locale: string) {
  if (typeof document === 'undefined') return
  document.documentElement.setAttribute('lang', locale)
  document.documentElement.setAttribute('dir', RTL_LOCALES.has(locale) ? 'rtl' : 'ltr')
}

export default defineNuxtPlugin(async (nuxtApp) => {
  const stored = (typeof localStorage !== 'undefined' && localStorage.getItem('bow.locale')) || null
  const initial = isLocale(stored) ? stored : 'en'

  const i18n = createI18n({
    legacy: false,
    globalInjection: true,
    // Start on English and switch below once the chosen catalogue has loaded.
    // Setting `locale` to a locale with no messages yet would render raw keys
    // for a frame (and, for he/ar, in the wrong direction).
    locale: 'en',
    fallbackLocale: 'en',
    missingWarn: false,
    fallbackWarn: false,
    messages: { en },
    datetimeFormats: perLocale(DATETIME_FORMATS),
    numberFormats: perLocale(NUMBER_FORMATS),
  })

  const loaded = new Set<string>(['en'])

  // Fetch and register a catalogue once. Safe to call repeatedly: already
  // loaded (or unknown) locales resolve immediately. A failed chunk fetch
  // leaves the locale unloaded so callers can fall back to English rather
  // than switching to a catalogue that isn't there.
  const loadLocaleMessages = async (code: string): Promise<boolean> => {
    if (loaded.has(code)) return true
    const loader = LOCALE_LOADERS[code]
    if (!loader) return false
    try {
      const mod = await loader()
      i18n.global.setLocaleMessage(code, (mod?.default ?? mod) as any)
      loaded.add(code)
      return true
    } catch (e) {
      console.warn(`Failed to load locale "${code}", staying on the current one:`, e)
      return false
    }
  }

  nuxtApp.vueApp.use(i18n)

  // Non-English users pay one extra chunk fetch here before the app renders.
  // That is the trade for English users (the default) no longer downloading
  // nine catalogues they will never read.
  const initialReady = initial === 'en' ? true : await loadLocaleMessages(initial)
  const effective = initialReady ? initial : 'en'
  ;(i18n.global.locale as any).value = effective
  applyDocumentLocale(effective)

  // Expose a small setter that also persists + updates <html> attrs.
  const setLocale = async (next: string) => {
    if (!isLocale(next)) return
    if (!(await loadLocaleMessages(next))) return
    ;(i18n.global.locale as any).value = next
    try { localStorage.setItem('bow.locale', next) } catch {}
    applyDocumentLocale(next)
  }

  nuxtApp.provide('setLocale', setLocale)
  // Callers that switch `i18n.locale` themselves (e.g. the "system default"
  // branch of the profile language picker, which must NOT persist a choice)
  // have to await this first or they will render raw keys.
  nuxtApp.provide('loadLocaleMessages', loadLocaleMessages)

  // Backend hydration (applying the org's configured locale when the user
  // has not picked their own) happens in `layouts/default.vue` after the
  // session + org are ready — the plugin can't do it reliably because the
  // org id lives in Nuxt state, not localStorage, and running useMyFetch
  // from here is racy.
})
