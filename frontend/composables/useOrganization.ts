// /composables/useOrganization.ts
export const useOrganization = () => {
  const router = useRouter()
  const route = useRoute()

  const { getSession } = useAuth()
  // Initialize with null to indicate not loaded yet
  const organization = useState('organization', () => ({
    id: null as string | null,
    name: '',
  }))

  // Fetch organization from session data
  const fetchOrganizationFromSession = async () => {
    const session = await getSession({ force: true })
    if (session?.organizations?.length > 0) {
      const firstOrg = session.organizations[0]
      organization.value.id = firstOrg.id
      organization.value.name = firstOrg.name
    }
    return organization.value
  }

  // Ensure organization is set
  const ensureOrganization = async () => {
    if (!organization.value?.id) {
      await fetchOrganizationFromSession()
      if (!organization.value?.id) {
        const route = useRoute()
        // Skip redirect if we're on the verify page or organization creation page
        if (!route.path.startsWith('/users/') && !route.path.startsWith('/organizations/')) {
          //router.push('/organizations/new')
        }
      }
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

  return {
    organization,
    ensureOrganization,
    fetchOrganization,
    fetchOrganizationFromSession,
  }
}
