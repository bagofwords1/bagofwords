export const usePermissions = () => {
  return useState<string[]>('permissions', () => [])
}

export const usePermissionsLoaded = () => {
  return useState<boolean>('permissionsLoaded', () => false)
}

export const useResourcePermissions = () => {
  return useState<Record<string, string[]>>('resourcePermissions', () => ({}))
}

// Check org-level or resource-level permissions
// Org-level:      useCan('view_reports')
// Resource-level: useCan('query', { type: 'data_source', id: '<uuid>' })
export const useCan = (permission: string, resource?: { type: string; id: string }) => {
  const permissions = usePermissions()
  const permissionsLoaded = usePermissionsLoaded()

  if (!permissionsLoaded.value) return false

  // full_admin_access bypasses all checks
  if (permissions.value.includes('full_admin_access')) return true

  if (!resource) {
    // Org-level check
    return permissions.value.includes(permission)
  }

  // Resource-level check
  const resourcePerms = useResourcePermissions()
  const key = `${resource.type}:${resource.id}`
  return resourcePerms.value[key]?.includes(permission) ?? false
}
