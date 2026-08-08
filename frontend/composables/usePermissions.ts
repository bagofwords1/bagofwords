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
  // Org connection-admin: manage every connection's config + create agents on any.
  manage_connections:  { connection: ['manage_connection', 'create_data_sources'] },
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
  connection: {
    // Managing all agents on a connection includes being able to create them.
    manage_data_sources: ['create_data_sources'],
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

// Org permissions that grant the org-wide monitoring console — mirrors
// CONSOLE_ADMIN_PERMISSIONS in app/core/console_access.py.
export const useHasOrgWideConsole = () =>
  useCan('manage_settings') || useCan('manage_connections')

// Who can open the monitoring console. Org admins get the org-wide view, and
// anyone who manages at least one agent gets the same console scoped to the
// agents they manage. Kept here so the sidebar entry, the tab strip and the
// page guards can't drift apart.
export const useCanAccessMonitoring = () =>
  useHasOrgWideConsole() || useCanAny('manage', 'data_source')

// ALL-of check: does the user hold `permission` on EVERY resource id in the
// list? Mirrors the backend's ALL-attached-agents rule for editing a shared
// instruction (check_resource_permissions fails on the first miss). Ported
// from PR #732.
//
// Empty list => the instruction is GLOBAL (attached to no agent), which stays
// an org-level capability: require org-level `permission` (full_admin_access
// bypasses everywhere via useCan).
// Usage: useCanAll('manage_instructions', 'data_source', instruction.data_sources.map(d => d.id))
export const useCanAll = (permission: string, resourceType: string, ids: string[]) => {
  const permissions = usePermissions()
  const permissionsLoaded = usePermissionsLoaded()

  if (!permissionsLoaded.value) return false
  if (permissions.value.includes('full_admin_access')) return true

  // Global (no attached resources): org-level permission only.
  if (!ids || ids.length === 0) return permissions.value.includes(permission)

  // Every attached resource must be individually manageable.
  return ids.every((id) => useCan(permission, { type: resourceType, id }))
}

type InstructionPermissionTarget = {
  data_sources?: Array<string | { id?: string | null }> | null
  data_source_ids?: string[] | null
}

// Authority over one instruction mirrors the backend's
// _require_instruction_authority rule exactly:
//   - attached instruction: manage_instructions on EVERY attached agent
//   - global instruction: org-level manage_instructions
//
// Callers must pass a detail object that explicitly carries one of the scope
// fields. Missing scope data fails closed instead of being mistaken for an
// empty (global) scope while an instruction fetch is still in flight.
export const useCanManageInstruction = (
  instruction?: InstructionPermissionTarget | null,
) => {
  if (!instruction) return false

  const hasDataSources = Array.isArray(instruction.data_sources)
  const hasDataSourceIds = Array.isArray(instruction.data_source_ids)
  if (!hasDataSources && !hasDataSourceIds) return false

  // Some responses expose hydrated objects, others expose raw ids, and a few
  // expose both. Union both representations so an empty hydrated list can
  // never mask non-empty raw scope ids.
  const rawIds: Array<string | { id?: string | null }> = [
    ...(hasDataSources ? instruction.data_sources! : []),
    ...(hasDataSourceIds ? instruction.data_source_ids! : []),
  ]
  const ids = [...new Set(rawIds
    .map((item) => typeof item === 'string' ? item : item?.id)
    .filter((id): id is string => typeof id === 'string' && id.length > 0))]

  return useCanAll('manage_instructions', 'data_source', ids)
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
