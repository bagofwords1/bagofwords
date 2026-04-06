// LDAP Sync Management Composable
// Licensed under the Business Source License 1.1

export type SyncResult = {
  groups_created: number
  groups_updated: number
  groups_removed: number
  memberships_added: number
  memberships_removed: number
  users_not_found: number
  errors: string[]
  timestamp: string | null
}

export type SyncStatus = {
  last_sync: SyncResult | null
  is_syncing: boolean
  ldap_configured: boolean
}

export type LDAPGroupPreview = {
  dn: string
  name: string
  member_count: number
  exists_in_app: boolean
  members_to_add: number
  members_to_remove: number
}

export type LDAPSyncPreview = {
  groups_to_create: number
  groups_to_update: number
  groups_to_remove: number
  total_membership_changes: number
  groups: LDAPGroupPreview[]
}

export type LDAPTestResult = {
  connected: boolean
  server: string
  vendor: string | null
  error: string | null
  user_count: number | null
  group_count: number | null
}

export const useLdapSync = () => {
  const status = ref<SyncStatus | null>(null)
  const preview = ref<LDAPSyncPreview | null>(null)
  const testResult = ref<LDAPTestResult | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  const fetchStatus = async () => {
    error.value = null
    try {
      const res = await useMyFetch('/api/enterprise/ldap/sync/status')
      if (res.status.value !== 'success') {
        const msg = (res.error?.value as any)?.data?.detail || 'Failed to fetch LDAP status'
        throw new Error(msg)
      }
      status.value = res.data.value as SyncStatus
    } catch (e: any) {
      error.value = e.message || 'Failed to fetch LDAP status'
    }
  }

  const triggerSync = async (): Promise<SyncResult | null> => {
    loading.value = true
    error.value = null
    try {
      const res = await useMyFetch('/api/enterprise/ldap/sync', {
        method: 'POST',
      })
      if (res.status.value !== 'success') {
        const msg = (res.error?.value as any)?.data?.detail || 'Sync failed'
        throw new Error(msg)
      }
      const result = res.data.value as SyncResult
      // Refresh status after sync
      await fetchStatus()
      return result
    } catch (e: any) {
      error.value = e.message || 'Sync failed'
      return null
    } finally {
      loading.value = false
    }
  }

  const fetchPreview = async (): Promise<LDAPSyncPreview | null> => {
    loading.value = true
    error.value = null
    try {
      const res = await useMyFetch('/api/enterprise/ldap/sync/preview')
      if (res.status.value !== 'success') {
        const msg = (res.error?.value as any)?.data?.detail || 'Failed to fetch preview'
        throw new Error(msg)
      }
      preview.value = res.data.value as LDAPSyncPreview
      return preview.value
    } catch (e: any) {
      error.value = e.message || 'Failed to fetch preview'
      return null
    } finally {
      loading.value = false
    }
  }

  const testConnection = async (): Promise<LDAPTestResult | null> => {
    loading.value = true
    error.value = null
    try {
      const res = await useMyFetch('/api/enterprise/ldap/test-connection')
      if (res.status.value !== 'success') {
        const msg = (res.error?.value as any)?.data?.detail || 'Connection test failed'
        throw new Error(msg)
      }
      testResult.value = res.data.value as LDAPTestResult
      return testResult.value
    } catch (e: any) {
      error.value = e.message || 'Connection test failed'
      return null
    } finally {
      loading.value = false
    }
  }

  return {
    status,
    preview,
    testResult,
    loading,
    error,
    fetchStatus,
    triggerSync,
    fetchPreview,
    testConnection,
  }
}
