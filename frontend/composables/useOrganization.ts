// /composables/useOrganization.ts
const STORAGE_KEY = 'bow.selectedOrganizationId'

export const useOrganization = () => {
  const { getSession, data: sessionData } = useAuth()
  // Initialize with null to indicate not loaded yet
  const organization = useState('organization', () => ({
    id: null as string | null,
    name: '',
  }))

  const readPersistedOrgId = (): string | null => {
    if (!process.client) return null
    try { return localStorage.getItem(STORAGE_KEY) } catch { return null }
  }

  const writePersistedOrgId = (id: string | null) => {
    if (!process.client) return
    try {
      if (id) localStorage.setItem(STORAGE_KEY, id)
      else localStorage.removeItem(STORAGE_KEY)
    } catch {}
  }

  // Fetch organization from session data.
  // Reuse the session already loaded by the auth middleware / permissions
  // plugin instead of forcing a fresh whoami — this used to fire an extra
  // (forced) whoami on every boot on top of the one just fetched. Only hit the
  // network when we genuinely have no session yet.
  const fetchOrganizationFromSession = async () => {
    const cached = sessionData?.value as any
    const session = (cached && cached.organizations) ? cached : await getSession()
    const orgs = session?.organizations || []
    if (orgs.length > 0) {
      const persistedId = readPersistedOrgId()
      const match = persistedId ? orgs.find((o: any) => o.id === persistedId) : null
      const chosen = match || orgs[0]
      organization.value.id = chosen.id
      organization.value.name = chosen.name
    }
    return organization.value
  }

  // Ensure organization is set
  const ensureOrganization = async () => {
    if (!organization.value?.id) {
      await fetchOrganizationFromSession()
    }
    return organization.value
  }

  // Fetch organization without redirecting
  const fetchOrganization = async () => {
    if (!organization.value?.id) {
      await fetchOrganizationFromSession()
    }
    return organization.value
  }

  // Switch active organization and reload so all org-scoped state is rebuilt
  const setOrganization = (orgId: string) => {
    if (!orgId || orgId === organization.value?.id) return
    writePersistedOrgId(orgId)
    if (process.client) {
      window.location.href = '/'
    }
  }

  return {
    organization,
    ensureOrganization,
    fetchOrganization,
    fetchOrganizationFromSession,
    setOrganization,
  }
}
