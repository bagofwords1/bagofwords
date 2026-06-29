export const usePermissions = () => {
  return useState<string[]>('permissions', () => [])
}

export const usePermissionsLoaded = () => {
  return useState<boolean>('permissionsLoaded', () => false)
}

export const useResourcePermissions = () => {
  return useState<Record<string, string[]>>('resourcePermissions', () => ({}))
}

// Mirror of backend ORG_PERM_IMPLIES_RESOURCE in app/core/permission_resolver.py.
// Holding any of these org perms implies the corresponding per-resource perm
// on every resource of that type.
const ORG_PERM_IMPLIES_RESOURCE: Record<string, Record<string, string[]>> = {
  manage_instructions: { data_source: ['manage_instructions'] },
  manage_entities:     { data_source: ['create_entities'] },
  manage_evals:        { data_source: ['manage_evals'] },
}

const isImpliedByOrgPerm = (
  orgPerms: string[],
  resourceType: string,
  permission: string,
): boolean => {
  for (const orgPerm of orgPerms) {
    const implied = ORG_PERM_IMPLIES_RESOURCE[orgPerm]?.[resourceType]
    if (implied && implied.includes(permission)) return true
  }
  return false
}

// Mirror of backend RESOURCE_PERM_IMPLIES in app/core/permission_resolver.py.
// A `manage` grant on a resource is a superset implying the per-resource
// management permissions — so an agent owner/manager sees the same edit
// affordances (instructions, entities, evals, members) the backend allows.
const RESOURCE_PERM_IMPLIES: Record<string, Record<string, string[]>> = {
  data_source: {
    manage: ['manage_instructions', 'create_entities', 'manage_evals', 'manage_members', 'view', 'view_schema'],
  },
}

const isImpliedByGrant = (
  grantedPerms: string[],
  resourceType: string,
  permission: string,
): boolean => {
  const byPerm = RESOURCE_PERM_IMPLIES[resourceType]
  if (!byPerm) return false
  for (const held of grantedPerms) {
    if (byPerm[held]?.includes(permission)) return true
  }
  return false
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

  // Resource-level check (with org-perm implication tier)
  if (isImpliedByOrgPerm(permissions.value, resource.type, permission)) return true

  const resourcePerms = useResourcePermissions()
  const key = `${resource.type}:${resource.id}`
  const granted = resourcePerms.value[key]
  if (!granted) return false
  // Superset grant (e.g. `manage`) implies the management sub-permissions.
  if (isImpliedByGrant(granted, resource.type, permission)) return true
  return granted.includes(permission)
}

// Two-tier OR check: org-level permission OR has it on ANY resource of given type.
// Use this for UI decisions like "show Create vs Suggest" where the user might
// have the permission scoped to specific data sources rather than org-wide.
// Usage: useCanAny('manage_instructions', 'data_source')
export const useCanAny = (permission: string, resourceType?: string) => {
  const permissions = usePermissions()
  const permissionsLoaded = usePermissionsLoaded()
  const resourcePerms = useResourcePermissions()

  if (!permissionsLoaded.value) return false
  if (permissions.value.includes('full_admin_access')) return true
  if (permissions.value.includes(permission)) return true

  if (!resourceType) return false

  // Implied by an admin org perm on this resource type
  if (isImpliedByOrgPerm(permissions.value, resourceType, permission)) return true

  // Check if ANY resource grant of this type includes the permission
  // (directly or via a superset grant like `manage`).
  for (const [key, perms] of Object.entries(resourcePerms.value)) {
    if (!key.startsWith(`${resourceType}:`)) continue
    if (perms.includes(permission) || isImpliedByGrant(perms, resourceType, permission)) {
      return true
    }
  }
  return false
}
