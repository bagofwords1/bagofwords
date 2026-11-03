import { computed, inject, type InjectionKey, type Ref } from 'vue'
import { useI18n } from 'vue-i18n'

export const responseLocaleKey: InjectionKey<Ref<string>> = Symbol('response-locale')

/** A response-scoped translator; outside a response it uses the app locale. */
export function useResponseI18n() {
  const global = useI18n({ useScope: 'global' })
  const responseLocale = inject(responseLocaleKey, null)
  const locale = computed(() => responseLocale?.value || global.locale.value)
  const t = ((key: string, values?: any, options?: any) => {
    const target = locale.value
    if (typeof values === 'number') return global.t(key, values, { ...options, locale: target })
    return global.t(key, values || {}, { ...options, locale: target })
  }) as typeof global.t
  return { ...global, locale, t }
}
