import { usePermissions, usePermissionsLoaded, useResourcePermissions } from '~/composables/usePermissions'

export default defineNuxtPlugin(async (nuxtApp) => {
  const { getSession } = useAuth()
  const { organization, ensureOrganization } = useOrganization()
  const permissions = usePermissions()
  const permissionsLoaded = usePermissionsLoaded()
  const resourcePermissions = useResourcePermissions()

  // Extract the permission loading logic into a reusable function
  const loadPermissions = async () => {
    try {
      const session = await getSession()
      await ensureOrganization()

      if (!session) {
        console.warn('Session data is undefined. Ensure the user is authenticated.')
        permissionsLoaded.value = true
        return
      }

      if (session.organizations && session.organizations.length > 0) {
        const org = session.organizations.find(
          (o: any) => o.id === organization.value.id
        )

        if (org?.permissions?.length) {
          // New path: server-supplied resolved permissions
          permissions.value = org.permissions
          resourcePermissions.value = org.resource_permissions || {}
        } else {
          // Fallback: old path for backward compat during migration
          const rolePermissions = getPermissionsForRole(org?.role)
          permissions.value = rolePermissions
          resourcePermissions.value = {}
        }
        permissionsLoaded.value = true
      } else {
        console.warn('No organizations found in session data.')
        permissionsLoaded.value = true
      }
    } catch (error) {
      console.error('Error fetching session data:', error)
      permissionsLoaded.value = true
    }
  }

  // Load permissions on initial app load
  await loadPermissions()

  // Add router hook to reload permissions on navigation
  nuxtApp.hook('app:mounted', () => {
    const router = useRouter()
    router.afterEach(async (to, from) => {
      // Only reload permissions if we're navigating to a different route
      // and permissions were previously loaded
      if (to.path !== from.path && permissionsLoaded.value) {
        permissionsLoaded.value = false // Reset loaded state
        await loadPermissions()
      }
    })
  })
})

// Fallback: minimal MVP perms used only if the server didn't return resolved
// permissions on whoami. Mirrors permissions_registry.DEFAULT_MEMBER_PERMISSIONS
// and uses the full_admin_access wildcard for admins.
function getPermissionsForRole(role: string): string[] {
  if (role === 'admin') return ['full_admin_access']
  return [
    'view_reports',
    'create_reports',
    'update_reports',
    'delete_reports',
    'publish_reports',
    'manage_files',
    'view_members',
  ]
}
