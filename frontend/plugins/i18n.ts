import { createI18n } from 'vue-i18n'
import en from '../../locales/en.json'
import es from '../../locales/es.json'
import he from '../../locales/he.json'

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
  return typeof x === 'string' && ['en', 'es', 'he'].includes(x)
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
    messages: { en, es, he },
    datetimeFormats: { en: DATETIME_FORMATS, es: DATETIME_FORMATS, he: DATETIME_FORMATS },
    numberFormats: { en: NUMBER_FORMATS, es: NUMBER_FORMATS, he: NUMBER_FORMATS },
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

  // Hydrate from backend once on client (server-provided config may override
  // the cached choice, which is desirable when an admin changes the org locale).
  try {
    const cfg = await $fetch<any>('/api/config/i18n', {
      headers: (() => {
        const headers: Record<string, string> = {}
        try {
          const orgId = localStorage.getItem('x-organization-id')
          if (orgId) headers['X-Organization-Id'] = orgId
        } catch {}
        return headers
      })(),
    })
    const next = cfg?.current_locale
    // Only apply if the user hasn't already picked a locale explicitly.
    if (!stored && isLocale(next)) {
      setLocale(next)
    }
  } catch {
    // ignore; first-boot fallback already in place
  }
})
