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

// Two-tier OR check: org-level permission OR has it on ANY resource of given type.
// Use this for UI decisions like "show Create vs Suggest" where the user might
// have the permission scoped to specific data sources rather than org-wide.
// Usage: useCanAny('create_instructions', 'data_source')
export const useCanAny = (permission: string, resourceType?: string) => {
  const permissions = usePermissions()
  const permissionsLoaded = usePermissionsLoaded()
  const resourcePerms = useResourcePermissions()

  if (!permissionsLoaded.value) return false
  if (permissions.value.includes('full_admin_access')) return true
  if (permissions.value.includes(permission)) return true

  if (!resourceType) return false

  // Check if ANY resource grant of this type includes the permission
  for (const [key, perms] of Object.entries(resourcePerms.value)) {
    if (key.startsWith(`${resourceType}:`) && perms.includes(permission)) {
      return true
    }
  }
  return false
}
