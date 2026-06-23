// Single source of truth for the fixed top banner (onboarding / license expiry)
// shown above the app in layouts/default.vue. Full-height views (e.g. the Agents
// KnowledgeExplorer) need to know whether the banner is present so they can
// subtract its height — otherwise a `100vh` view sits 40px below the banner and
// its bottom (and anything pinned there) is pushed off-screen.
import { useCan } from '~/composables/usePermissions'

// Matches the layout's `top-10` / `pt-10` (Tailwind h-10 = 2.5rem = 40px).
export const TOP_BANNER_HEIGHT = '2.5rem'

export const useTopBanner = () => {
  const { onboarding } = useOnboarding()
  const { isExpired, isExpiringSoon } = useEnterprise()
  const canModifySettings = computed(() => useCan('manage_settings'))

  const showGlobalOnboardingBanner = computed<boolean>(() => {
    if (!canModifySettings.value) return false
    const ob = onboarding.value as any
    if (!ob) return false
    const steps = ob.steps || {}
    const llmDone = steps.llm_configured?.status === 'done'
    const dataDone = steps.data_source_created?.status === 'done'
    return !(llmDone && dataDone)
  })

  const showLicenseBanner = computed<boolean>(() => {
    // Never stack on top of the onboarding banner — they share the same slot.
    if (showGlobalOnboardingBanner.value) return false
    return isExpired.value || isExpiringSoon.value
  })

  // Either fixed top banner pushes the sidebar + content down by the same amount.
  const showTopBanner = computed<boolean>(() => showGlobalOnboardingBanner.value || showLicenseBanner.value)

  return { showGlobalOnboardingBanner, showLicenseBanner, showTopBanner, bannerHeight: TOP_BANNER_HEIGHT }
}
