import { usePermissions, usePermissionsLoaded, useResourcePermissions } from '~/composables/usePermissions'

export default defineNuxtPlugin(async (nuxtApp) => {
  const { getSession, data: sessionData } = useAuth()
  const { organization, ensureOrganization } = useOrganization()
  const permissions = usePermissions()
  const permissionsLoaded = usePermissionsLoaded()
  const resourcePermissions = useResourcePermissions()

  // Resolve the active org's permissions from an already-fetched session.
  // Pure (no network): safe to call from a reactive watcher without looping.
  // Returns true once the active org was matched and its permissions applied.
  const resolveFromSession = (session: any): boolean => {
    if (!session?.organizations?.length) return false

    const org = session.organizations.find(
      (o: any) => o.id === organization.value.id
    )

    // The active org may not be matchable yet (id not set, or the session
    // predates the current selection). Don't fall back to member-only perms —
    // that would strip an admin's `full_admin_access` and hide admin-gated
    // items. Leave the existing set in place; the watcher re-resolves once the
    // session or org settles.
    if (!org) return false

    if (org.permissions?.length) {
      // New path: server-supplied resolved permissions
      permissions.value = org.permissions
      resourcePermissions.value = org.resource_permissions || {}
    } else {
      // Fallback: old path for backward compat during migration
      permissions.value = getPermissionsForRole(org.role)
      resourcePermissions.value = {}
    }
    permissionsLoaded.value = true
    return true
  }

  // Initial load: fetch the session (and ensure an org is selected) once.
  const loadPermissions = async () => {
    try {
      const session = await getSession()
      await ensureOrganization()
      resolveFromSession(session)
    } catch (error) {
      console.error('Error fetching session data:', error)
    } finally {
      // Always unblock the app even if the org couldn't be matched yet — gated
      // items stay hidden until the watcher resolves real permissions, but the
      // rest of the UI (which keys off `permissionsLoaded`) can render.
      permissionsLoaded.value = true
    }
  }

  // Load permissions on initial app load. Keep permissions stable after that:
  // toggling `permissionsLoaded` during every route change removes/reinserts
  // permission-gated sidebar links while Nuxt is also patching the page.
  // Under fast navigation this can trip Vue's Suspense DOM moves.
  await loadPermissions()

  // Self-heal stale/partial permissions. The initial load can race: if the
  // first whoami resolves before `organization.value.id` is set, the org
  // lookup misses and an admin is left without `full_admin_access` — silently
  // hiding admin-gated sidebar items (Monitoring, Evals) until a full reload.
  // The session refreshes on window focus (`enableRefreshOnWindowFocus`) and
  // the active org can change, so re-resolve from the latest session whenever
  // either settles. Resolving is pure (no fetch), so watching `sessionData`
  // can't loop, and we only ever set `permissionsLoaded` true (never back to
  // false), so the permission set updates in place without flickering links.
  watch(
    [sessionData, () => organization.value?.id],
    () => { resolveFromSession(sessionData.value) },
  )
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
