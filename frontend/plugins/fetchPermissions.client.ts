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

// Fallback: define the function to get permissions based on role (backward compat)
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
      'export_query',
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
      'manage_organization_settings',
      'create_entities',
      'update_entities',
      'delete_entities',
      'view_entities',
      'refresh_entities',
      'approve_entities',
      'reject_entities',
      'manage_evals',
      'run_evals',
      'view_evals',
      'view_builds',
      'create_builds',
      'manage_connections',
      'view_connections',
      'train_mode',
      'view_audit_logs',
      'manage_scim',
      'manage_roles',
      'manage_groups',
      'manage_role_assignments',
      'manage_resource_grants',
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
      'export_query',
      'view_organizations',
      'view_llm_settings',
      'view_organization_members',
      'view_instructions',
      'view_global_instructions',
      'suggest_instructions',
      'create_completion_feedback',
      'view_entities',
      'refresh_entities',
      'suggest_entities',
      'withdraw_entities',
      'view_evals',
      'run_evals',
      'view_builds',
    ],
  }

  return rolePermissionsMap[role] || []
}
