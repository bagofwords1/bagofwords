// /composables/useOnboarding.ts
import type { Ref } from 'vue'

type OnboardingStatus = 'pending' | 'done' | 'skipped'
type OnboardingStepKey = 'organization_created' | 'llm_configured' | 'data_source_created' | 'schema_selected' | 'instructions_added'

interface OnboardingStepStatus {
  status: OnboardingStatus
  ts?: string
}

interface OnboardingConfig {
  version: string
  current_step?: OnboardingStepKey | null
  completed: boolean
  dismissed: boolean
  steps: Record<OnboardingStepKey, OnboardingStepStatus>
}

interface OnboardingResponse {
  onboarding: OnboardingConfig
}

export const useOnboarding = () => {
  const state = useState<OnboardingConfig | null>('onboarding', () => null)

  const fetchOnboarding = async (opts?: { in_onboarding?: boolean; force?: boolean }) => {
    // Onboarding state is admin-only on the backend (manage_settings).
    // Skip the call entirely for non-admins to avoid 403 noise on every
    // route navigation (this composable is fetched from a global middleware,
    // the default layout, the home page, and the onboarding screens).
    // `force` lets callers that have already established admin-ness via another
    // signal (e.g. the onboarding middleware, which runs before permissions
    // are loaded) bypass the useCan gate and avoid a race that briefly renders
    // the home page before redirecting to onboarding.
    if (!opts?.force && !useCan('manage_settings')) return state.value

    const res = await useMyFetch('/organization/onboarding', {
      params: { in_onboarding: opts?.in_onboarding ? 'true' : 'false' }
    })
    if (res.error.value) throw res.error.value
    const data = (res.data as Ref<OnboardingResponse | null>).value
    if (data) state.value = data.onboarding
    return state.value
  }

  const updateOnboarding = async (payload: Partial<Pick<OnboardingConfig, 'dismissed' | 'completed' | 'current_step'>>) => {
    const res = await useMyFetch('/organization/onboarding', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
    if (res.error.value) throw res.error.value
    const data = (res.data as Ref<OnboardingResponse | null>).value
    if (data) state.value = data.onboarding
    return state.value
  }

  return { onboarding: state, fetchOnboarding, updateOnboarding }
}


