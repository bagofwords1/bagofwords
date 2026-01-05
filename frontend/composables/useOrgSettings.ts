// /composables/useOrgSettings.ts

type Feature = {
  name: string
  description: string
  is_lab?: boolean
  editable?: boolean
  state?: 'enabled' | 'disabled' | 'locked'
  value?: boolean | number | string | null
}

type OrganizationSettingsResponse = {
  id: string
  organization_id: string
  created_at: string
  updated_at: string
  config: {
    general?: Record<string, any>
    ai_features?: Record<string, Feature>
    allow_llm_see_data?: Feature
    allow_file_upload?: Feature
    allow_code_editing?: Feature
    enable_llm_judgement?: Feature
    [key: string]: any
  }
}

export const useOrgSettings = () => {
  const { organization, ensureOrganization } = useOrganization()

  // Cache settings per organization id
  const key = computed(() => `org-settings-${organization.value?.id || 'none'}`)
  const settings = useState<OrganizationSettingsResponse | null>(key.value, () => null)
  const loading = useState<boolean>(`${key.value}-loading`, () => false)
  const error = useState<string | null>(`${key.value}-error`, () => null)

  const fetchSettings = async () => {
    error.value = null
    loading.value = true
    try {
      await ensureOrganization()
      const res = await useMyFetch('/api/organization/settings')
      if (res.status.value !== 'success') {
        const msg = (res.error?.value as any)?.data?.message || 'Failed to fetch organization settings'
        throw new Error(msg)
      }
      settings.value = res.data.value as OrganizationSettingsResponse
    } catch (e: any) {
      error.value = e.message || 'Failed to fetch organization settings'
    } finally {
      loading.value = false
    }
  }

  // Auto-fetch when org id becomes available
  watch(
    () => organization.value?.id,
    async (newId, oldId) => {
      if (newId && newId !== oldId) {
        await fetchSettings()
      }
    },
    { immediate: true }
  )

  const getFeature = (path: string): Feature | undefined => {
    const cfg = settings.value?.config
    if (!cfg) return undefined
    // direct key first
    if (cfg[path]) return cfg[path] as Feature
    // ai_features map fallback
    const ai = cfg.ai_features || {}
    return ai[path]
  }

  const featureEnabled = (feature?: Feature) => {
    if (!feature) return false
    if (feature.state && feature.state !== 'enabled') return false
    if (typeof feature.value === 'boolean') return feature.value === true
    return feature.state === 'enabled'
  }

  const isJudgeEnabled = computed(() => featureEnabled(getFeature('enable_llm_judgement')))
  const canUploadFiles = computed(() => featureEnabled(getFeature('enable_file_upload')))
  const canEditCode = computed(() => featureEnabled(getFeature('enable_code_editing')))
  const isMcpEnabled = computed(() => featureEnabled(getFeature('mcp_enabled')))
  const allowLlmSeeData = computed(() => featureEnabled(getFeature('allow_llm_see_data')))

  return {
    settings,
    loading,
    error,
    fetchSettings,
    // flags
    isJudgeEnabled,
    canUploadFiles,
    canEditCode,
    isMcpEnabled,
    allowLlmSeeData,
    // raw accessor
    getFeature,
  }
}


