import { computed, toValue, type MaybeRefOrGetter } from 'vue'
import { useI18n } from 'vue-i18n'

/**
 * Browser tab titles.
 *
 * Every tab reads `<page> · Bag of Words`. Two pieces make that happen:
 *
 * 1. `plugins/page-title.ts` sets the `· Bag of Words` template once, for the
 *    whole app, and a default title from the route: a page declares
 *    `definePageMeta({ titleKey: 'pageTitles.signIn' })` and the plugin
 *    translates it (and re-translates it when the locale changes). Pages in
 *    the settings and monitoring layouts get their section appended
 *    (`Access · Settings · Bag of Words`), see `LAYOUT_SECTION_KEYS`.
 *
 * 2. A page (or component) that shows a record calls `usePageTitle()` with the
 *    record's name. Its title is registered after the plugin's, so it wins
 *    while that page is mounted, and the route default comes back when it
 *    unmounts — leaving a report no longer leaves a bare URL in the tab.
 *
 * Only pass a name that came from a successful fetch: a user who cannot open
 * a record must not see its name in the tab, history or bookmarks.
 */
export const APP_TITLE = 'Bag of Words'
export const TITLE_SEPARATOR = ' · '

export function formatTitle(page?: string | null): string {
  const trimmed = (page || '').trim()
  return trimmed ? `${trimmed}${TITLE_SEPARATOR}${APP_TITLE}` : APP_TITLE
}

export function usePageTitle(
  name: MaybeRefOrGetter<string | null | undefined>,
  options: {
    /** While true (and no name yet), the tab says "Loading…". */
    loading?: MaybeRefOrGetter<boolean | undefined>
    /** Translation key for a record that has no name, or failed to load. */
    fallbackKey?: string
  } = {},
) {
  const { t } = useI18n()
  const title = computed(() => {
    const value = (toValue(name) || '').trim()
    if (value) return value
    if (toValue(options.loading)) return t('pageTitles.loading')
    return options.fallbackKey ? t(options.fallbackKey) : null
  })
  // No title → no `title` key at all, so the route default
  // (plugins/page-title.ts) shows through. `{ title: null }` would not do
  // that: unhead treats a later null title as "clear it", and the tab would
  // read a bare "Bag of Words".
  useHead(() => (title.value ? { title: title.value } : {}))
  return title
}
