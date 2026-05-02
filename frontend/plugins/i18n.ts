import { createI18n } from 'vue-i18n'
import en from '../../locales/en.json'
import es from '../../locales/es.json'
import he from '../../locales/he.json'
import fr from '../../locales/fr.json'
import sv from '../../locales/sv.json'
import ar from '../../locales/ar.json'
import ru from '../../locales/ru.json'
import de from '../../locales/de.json'
import pt from '../../locales/pt.json'
import it from '../../locales/it.json'

const RTL_LOCALES = new Set(['he', 'ar', 'fa', 'ur'])

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

function isLocale(x: unknown): x is string {
  return typeof x === 'string' && ['en', 'es', 'he', 'fr', 'sv', 'ar', 'ru', 'de', 'pt', 'it'].includes(x)
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
    locale: initial,
    fallbackLocale: 'en',
    missingWarn: false,
    fallbackWarn: false,
    messages: { en, es, he, fr, sv, ar, ru, de, pt, it },
    datetimeFormats: { en: DATETIME_FORMATS, es: DATETIME_FORMATS, he: DATETIME_FORMATS, fr: DATETIME_FORMATS, sv: DATETIME_FORMATS, ar: DATETIME_FORMATS, ru: DATETIME_FORMATS, de: DATETIME_FORMATS, pt: DATETIME_FORMATS, it: DATETIME_FORMATS },
    numberFormats: { en: NUMBER_FORMATS, es: NUMBER_FORMATS, he: NUMBER_FORMATS, fr: NUMBER_FORMATS, sv: NUMBER_FORMATS, ar: NUMBER_FORMATS, ru: NUMBER_FORMATS, de: NUMBER_FORMATS, pt: NUMBER_FORMATS, it: NUMBER_FORMATS },
  })

  nuxtApp.vueApp.use(i18n)
  applyDocumentLocale(initial)

  // Expose a small setter that also persists + updates <html> attrs.
  const setLocale = (next: string) => {
    if (!isLocale(next)) return
    ;(i18n.global.locale as any).value = next
    try { localStorage.setItem('bow.locale', next) } catch {}
    applyDocumentLocale(next)
  }

  nuxtApp.provide('setLocale', setLocale)

  // Backend hydration (applying the org's configured locale when the user
  // has not picked their own) happens in `layouts/default.vue` after the
  // session + org are ready — the plugin can't do it reliably because the
  // org id lives in Nuxt state, not localStorage, and running useMyFetch
  // from here is racy.
})
