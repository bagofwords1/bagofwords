import { formatTitle, TITLE_SEPARATOR } from '~/composables/usePageTitle'

// Pages in these layouts are one tab of a section; the section name follows
// the page's own title: "Access · Settings · Bag of Words".
const LAYOUT_SECTION_KEYS: Record<string, string> = {
  settings: 'nav.settings',
  monitoring: 'monitoring.title',
}

/**
 * The app-wide title: the `· Bag of Words` template and a default title from
 * the route's `titleKey` meta. See composables/usePageTitle.ts.
 *
 * Runs after plugins/i18n.ts (plugins load in file-name order), but resolves
 * `$t` lazily anyway so the order never matters.
 */
export default defineNuxtPlugin({
  name: 'page-title',
  setup(nuxtApp) {
    const router = useRouter()

    const title = computed(() => {
      const $t = nuxtApp.vueApp.config.globalProperties.$t as ((key: string) => string) | undefined
      if (!$t) return null
      const meta = router.currentRoute.value.meta as Record<string, unknown>
      const key = typeof meta.titleKey === 'string' ? meta.titleKey : null
      if (!key) return null
      const parts = [$t(key)]
      const section = typeof meta.layout === 'string' ? LAYOUT_SECTION_KEYS[meta.layout] : undefined
      if (section) parts.push($t(section))
      return parts.join(TITLE_SEPARATOR)
    })

    useHead({
      title,
      titleTemplate: (page?: string | null) => formatTitle(page),
    })
  },
})
