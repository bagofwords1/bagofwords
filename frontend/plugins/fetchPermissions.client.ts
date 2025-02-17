import { usePermissions } from '~/composables/usePermissions'
export default defineNuxtPlugin(async (nuxtApp) => {
  try {
    const { getSession } = useAuth()
    const { organization, ensureOrganization } = useOrganization()
    const permissions = usePermissions()

    const session = await getSession()
    await ensureOrganization()

    if (!session) {
      console.warn('Session data is undefined. Ensure the user is authenticated.')
      return
    }

    if (session.organizations && session.organizations.length > 0) {
      const userRole = session.organizations.find(
        (org) => org.id === organization.value.id
      )?.role
      const rolePermissions = getPermissionsForRole(userRole)
      permissions.value = rolePermissions
    } else {
      console.warn('No organizations found in session data.')
    }
  } catch (error) {
    console.error('Error fetching session data:', error)
  }
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
      'view_organizations'
    ],
    // Add more roles and permissions as needed
  }

  return rolePermissionsMap[role] || []
}
