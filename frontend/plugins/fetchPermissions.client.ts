import { usePermissions, usePermissionsLoaded } from '~/composables/usePermissions'
export default defineNuxtPlugin(async (nuxtApp) => {
  try {
    const { getSession } = useAuth()
    const { organization, ensureOrganization } = useOrganization()
    const permissions = usePermissions()
    const permissionsLoaded = usePermissionsLoaded()

    const session = await getSession()
    await ensureOrganization()

    if (!session) {
      console.warn('Session data is undefined. Ensure the user is authenticated.')
      permissionsLoaded.value = true // Set to true even if no session
      return
    }

    if (session.organizations && session.organizations.length > 0) {
      const userRole = session.organizations.find(
        (org) => org.id === organization.value.id
      )?.role
      const rolePermissions = getPermissionsForRole(userRole)
      permissions.value = rolePermissions
      permissionsLoaded.value = true // Mark as loaded
    } else {
      console.warn('No organizations found in session data.')
      permissionsLoaded.value = true // Set to true even if no organizations
    }
  } catch (error) {
    console.error('Error fetching session data:', error)
    const permissionsLoaded = usePermissionsLoaded()
    permissionsLoaded.value = true // Set to true even on error to avoid infinite loading
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
      'manage_data_source_memberships'
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
      'create_completion_feedback'
    ],
    // Add more roles and permissions as needed
  }

  return rolePermissionsMap[role] || []
}
