// Fetches and caches app-level settings from GET /api/settings
// (distinct from org-level settings in useOrgSettings)

type AppSettings = {
  smtp_enabled: boolean
  version: string
  environment: string
  base_url: string
  [key: string]: any
}

export const useAppSettings = () => {
  const settings = useState<AppSettings | null>('app-settings', () => null)
  const loading = useState<boolean>('app-settings-loading', () => false)

  const fetchSettings = async () => {
    if (settings.value) return // already cached
    loading.value = true
    try {
      const res = await useMyFetch('/api/settings')
      if (res.data.value) {
        settings.value = res.data.value as AppSettings
      }
    } catch {
      // Silent fail — features degrade gracefully
    } finally {
      loading.value = false
    }
  }

  const smtpEnabled = computed(() => settings.value?.smtp_enabled ?? false)

  // Auto-fetch on first use
  if (!settings.value && !loading.value) {
    fetchSettings()
  }

  return {
    settings,
    loading,
    smtpEnabled,
    fetchSettings,
  }
}
