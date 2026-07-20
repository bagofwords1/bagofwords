// Entra ID profile / job-info sync settings composable.
// Reads/writes the per-org toggle configured on the Identity Providers page.

export type EntraProfileSync = {
  enabled: boolean
  fields: string[]
}

// Graph /me fields readable with the default-granted User.Read scope. Kept in
// sync with ENTRA_PROFILE_SYNC_ALLOWED_FIELDS on the backend.
export const ENTRA_PROFILE_FIELDS = [
  'jobTitle',
  'department',
  'companyName',
  'officeLocation',
  'employeeId',
  'employeeType',
  'employeeHireDate',
  'employeeOrgData',
  'mobilePhone',
  'city',
  'state',
  'country',
  'usageLocation',
  'preferredLanguage',
]

export type EntraProfilePreview = {
  connected: boolean
  samples: Record<string, any>
  allowed_fields: string[]
  error: string | null
}

export const useEntraProfileSync = () => {
  const config = ref<EntraProfileSync | null>(null)
  const preview = ref<EntraProfilePreview | null>(null)
  const loading = ref(false)
  const previewLoading = ref(false)
  const saving = ref(false)
  const error = ref<string | null>(null)

  const fetchConfig = async () => {
    loading.value = true
    error.value = null
    try {
      const res = await useMyFetch('/api/organization/identity/entra-profile-sync')
      if (res.status.value !== 'success') {
        const msg = (res.error?.value as any)?.data?.detail || 'Failed to load Entra profile sync settings'
        throw new Error(msg)
      }
      config.value = res.data.value as EntraProfileSync
    } catch (e: any) {
      error.value = e.message || 'Failed to load Entra profile sync settings'
      config.value = null
    } finally {
      loading.value = false
    }
  }

  const save = async (payload: EntraProfileSync): Promise<boolean> => {
    saving.value = true
    error.value = null
    try {
      const res = await useMyFetch('/api/organization/identity/entra-profile-sync', {
        method: 'PUT',
        body: payload,
      })
      if (res.status.value !== 'success') {
        const msg = (res.error?.value as any)?.data?.detail || 'Failed to save Entra profile sync settings'
        throw new Error(msg)
      }
      config.value = res.data.value as EntraProfileSync
      return true
    } catch (e: any) {
      error.value = e.message || 'Failed to save Entra profile sync settings'
      return false
    } finally {
      saving.value = false
    }
  }

  const fetchPreview = async () => {
    previewLoading.value = true
    try {
      const res = await useMyFetch('/api/organization/identity/entra-profile-sync/preview')
      if (res.status.value !== 'success') {
        const msg = (res.error?.value as any)?.data?.detail || 'Failed to load profile preview'
        throw new Error(msg)
      }
      preview.value = res.data.value as EntraProfilePreview
    } catch (e: any) {
      preview.value = { connected: false, samples: {}, allowed_fields: ENTRA_PROFILE_FIELDS, error: e.message || 'error' }
    } finally {
      previewLoading.value = false
    }
  }

  return { config, preview, loading, previewLoading, saving, error, fetchConfig, save, fetchPreview }
}
