import { usePermissions, usePermissionsLoaded } from '~/composables/usePermissions'

export default defineNuxtPlugin(async (nuxtApp) => {
  const { getSession } = useAuth()
  const { organization, ensureOrganization } = useOrganization()
  const permissions = usePermissions()
  const permissionsLoaded = usePermissionsLoaded()

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
        const userRole = session.organizations.find(
          (org) => org.id === organization.value.id
        )?.role
        const rolePermissions = getPermissionsForRole(userRole)
        permissions.value = rolePermissions
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
        console.log('Navigation detected, reloading permissions...')
        permissionsLoaded.value = false // Reset loaded state
        await loadPermissions()
      }
    })
  })
})

// Define the function to get permissions based on role
function getPermissionsForRole(role: string): string[] {
  const rolePermissionsMap: Record<string, string[]> = {
    admin: [
      'create_data_source',
      'delete_data_source',
      'update_data_source',
      'view_settings',
      'add_organization_members',
      'update_organization_members',
      'remove_organization_members',
      'view_organization_members',
      'view_data_source',
      'view_reports',
      'create_reports',
      'update_reports',
      'delete_reports',
      'publish_reports',
      'rerun_report_steps',
      'view_files',
      'upload_files',
      'delete_files',
      'export_widgets',
      'create_text_widgets',
      'update_text_widgets',
      'delete_text_widgets',
      'create_widgets',
      'update_widgets',
      'delete_widgets',
      'view_widgets',
      'view_memories',
      'create_memories',
      'update_memories',
      'delete_memories',
      'rerun_memory_step',
      'view_organizations',
      'manage_llm_settings',
      'view_completion_plan',
      'view_organization_overview',
      'manage_organization_external_platforms',
      'view_llm_settings',
      'view_console',
      'view_instructions',
      'create_instructions',
      'update_instructions',
      'delete_instructions',
      'view_hidden_instructions',
      'manage_data_source_memberships',
      'modify_settings',
      'create_entities',
      'update_entities',
      'delete_entities',
      'view_entities',
      'refresh_entities',
      'approve_entities',
      'reject_entities'
    ],
    member: [
      'view_data_source',
      'view_reports',
      'create_reports',
      'update_reports',
      'delete_reports',
      'publish_reports',
      'rerun_report_steps',
      'view_files',
      'upload_files',
      'delete_files',
      'export_widgets',
      'create_text_widgets',
      'update_text_widgets',
      'delete_text_widgets',
      'create_widgets',
      'update_widgets',
      'delete_widgets',
      'view_widgets',
      'view_memories',
      'create_memories',
      'update_memories',
      'delete_memories',
      'rerun_memory_step',
      'view_organizations',
      'view_llm_settings',
      'view_organization_members',
      'view_instructions',
      'create_private_instructions',
      'create_completion_feedback',
      'view_entities',
      'refresh_entities',
      'suggest_entities',
      'withdraw_entities'
    ],
    // Add more roles and permissions as needed
  }

  return rolePermissionsMap[role] || []
}
