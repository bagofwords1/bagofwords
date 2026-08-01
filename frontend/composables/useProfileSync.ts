// Identity-provider profile sync settings composable, shared by the Entra ID
// and Google sections on the Identity Providers page. Reads/writes a per-org
// toggle + field selection and fetches live sample values from the signed-in
// admin's own profile.

export type ProfileSyncConfig = {
  enabled: boolean
  fields: string[]
}

export type ProfileSyncPreview = {
  connected: boolean
  samples: Record<string, any>
  allowed_fields: string[]
  error: string | null
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

// Google fields readable with the login-granted openid/profile/email scopes
// (userinfo + People API). Kept in sync with
// GOOGLE_PROFILE_SYNC_ALLOWED_FIELDS on the backend.
export const GOOGLE_PROFILE_FIELDS = [
  'displayName',
  'jobTitle',
  'department',
  'organization',
  'location',
  'locale',
  'hostedDomain',
]

export const useProfileSync = (endpoint: string, fallbackFields: string[]) => {
  const config = ref<ProfileSyncConfig | null>(null)
  const preview = ref<ProfileSyncPreview | null>(null)
  const loading = ref(false)
  const previewLoading = ref(false)
  const saving = ref(false)
  const error = ref<string | null>(null)

  const fetchConfig = async () => {
    loading.value = true
    error.value = null
    try {
      const res = await useMyFetch(endpoint)
      if (res.status.value !== 'success') {
        const msg = (res.error?.value as any)?.data?.detail || 'Failed to load profile sync settings'
        throw new Error(msg)
      }
      config.value = res.data.value as ProfileSyncConfig
    } catch (e: any) {
      error.value = e.message || 'Failed to load profile sync settings'
      config.value = null
    } finally {
      loading.value = false
    }
  }

  const save = async (payload: ProfileSyncConfig): Promise<boolean> => {
    saving.value = true
    error.value = null
    try {
      const res = await useMyFetch(endpoint, {
        method: 'PUT',
        body: payload,
      })
      if (res.status.value !== 'success') {
        const msg = (res.error?.value as any)?.data?.detail || 'Failed to save profile sync settings'
        throw new Error(msg)
      }
      config.value = res.data.value as ProfileSyncConfig
      return true
    } catch (e: any) {
      error.value = e.message || 'Failed to save profile sync settings'
      return false
    } finally {
      saving.value = false
    }
  }

  const fetchPreview = async () => {
    previewLoading.value = true
    try {
      const res = await useMyFetch(`${endpoint}/preview`)
      if (res.status.value !== 'success') {
        const msg = (res.error?.value as any)?.data?.detail || 'Failed to load profile preview'
        throw new Error(msg)
      }
      preview.value = res.data.value as ProfileSyncPreview
    } catch (e: any) {
      preview.value = { connected: false, samples: {}, allowed_fields: fallbackFields, error: e.message || 'error' }
    } finally {
      previewLoading.value = false
    }
  }

  return { config, preview, loading, previewLoading, saving, error, fetchConfig, save, fetchPreview }
}
