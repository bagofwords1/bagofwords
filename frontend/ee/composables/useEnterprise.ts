// Enterprise License Composable
// Licensed under the Business Source License 1.1

export type LicenseInfo = {
  licensed: boolean
  tier: string
  org_name: string | null
  expires_at: string | null
  features: string[]
  license_id: string | null
}

export const useEnterprise = () => {
  const license = useState<LicenseInfo | null>('enterprise-license', () => null)
  const loading = useState<boolean>('enterprise-license-loading', () => false)
  const error = useState<string | null>('enterprise-license-error', () => null)
  const initialized = useState<boolean>('enterprise-license-initialized', () => false)

  const fetchLicense = async () => {
    if (loading.value) return

    loading.value = true
    error.value = null

    try {
      const res = await useMyFetch('/api/license')
      if (res.status.value !== 'success') {
        const msg = (res.error?.value as any)?.data?.message || 'Failed to fetch license info'
        throw new Error(msg)
      }
      license.value = res.data.value as LicenseInfo
      initialized.value = true
    } catch (e: any) {
      error.value = e.message || 'Failed to fetch license info'
      // Set default community license on error
      license.value = {
        licensed: false,
        tier: 'community',
        org_name: null,
        expires_at: null,
        features: [],
        license_id: null,
      }
      initialized.value = true
    } finally {
      loading.value = false
    }
  }

  // Fetch license on first use
  if (!initialized.value && !loading.value) {
    fetchLicense()
  }

  const isLicensed = computed(() => license.value?.licensed ?? false)
  const tier = computed(() => license.value?.tier ?? 'community')
  const isExpired = computed(() => license.value?.tier === 'expired')

  const hasFeature = (feature: string): boolean => {
    if (!license.value?.licensed) return false
    // If no specific features listed, all features are available
    if (!license.value.features || license.value.features.length === 0) return true
    return license.value.features.includes(feature)
  }

  const expiresAt = computed(() => {
    if (!license.value?.expires_at) return null
    return new Date(license.value.expires_at)
  })

  const daysUntilExpiry = computed(() => {
    if (!expiresAt.value) return null
    const now = new Date()
    const diff = expiresAt.value.getTime() - now.getTime()
    return Math.ceil(diff / (1000 * 60 * 60 * 24))
  })

  const isExpiringSoon = computed(() => {
    if (!daysUntilExpiry.value) return false
    return daysUntilExpiry.value <= 30 && daysUntilExpiry.value > 0
  })

  return {
    license,
    loading,
    error,
    fetchLicense,
    // Computed
    isLicensed,
    tier,
    isExpired,
    expiresAt,
    daysUntilExpiry,
    isExpiringSoon,
    // Methods
    hasFeature,
  }
}
