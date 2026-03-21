// SCIM Token Management Composable
// Licensed under the Business Source License 1.1

export type ScimToken = {
  id: string
  name: string
  token_prefix: string
  created_at: string | null
  expires_at: string | null
  last_used_at: string | null
}

export type ScimTokenCreated = ScimToken & {
  token: string
}

export const useScimTokens = () => {
  const tokens = ref<ScimToken[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  const fetchTokens = async () => {
    loading.value = true
    error.value = null

    try {
      const res = await useMyFetch('/api/enterprise/scim/tokens')

      if (res.status.value !== 'success') {
        const msg = (res.error?.value as any)?.data?.detail || 'Failed to fetch SCIM tokens'
        throw new Error(msg)
      }

      tokens.value = res.data.value as ScimToken[]
    } catch (e: any) {
      error.value = e.message || 'Failed to fetch SCIM tokens'
      tokens.value = []
    } finally {
      loading.value = false
    }
  }

  const createToken = async (name: string): Promise<ScimTokenCreated | null> => {
    try {
      const res = await useMyFetch('/api/enterprise/scim/tokens', {
        method: 'POST',
        body: { name },
      })

      if (res.status.value !== 'success') {
        const msg = (res.error?.value as any)?.data?.detail || 'Failed to create SCIM token'
        throw new Error(msg)
      }

      const created = res.data.value as ScimTokenCreated
      await fetchTokens()
      return created
    } catch (e: any) {
      error.value = e.message || 'Failed to create SCIM token'
      return null
    }
  }

  const revokeToken = async (tokenId: string): Promise<boolean> => {
    try {
      const res = await useMyFetch(`/api/enterprise/scim/tokens/${tokenId}`, {
        method: 'DELETE',
      })

      if (res.status.value !== 'success') {
        const msg = (res.error?.value as any)?.data?.detail || 'Failed to revoke SCIM token'
        throw new Error(msg)
      }

      await fetchTokens()
      return true
    } catch (e: any) {
      error.value = e.message || 'Failed to revoke SCIM token'
      return false
    }
  }

  return {
    tokens,
    loading,
    error,
    fetchTokens,
    createToken,
    revokeToken,
  }
}
