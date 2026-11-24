// Whether the signed-in member may use the BOW workspace in the active
// organization, or is an *app user*: a member whose roles withhold
// `access_workspace`, who has only the apps shared with them (/apps, /d/…).
//
// Read from the session (whoami), not from `usePermissions()`: that state is
// also empty while the active organization has not been matched yet, and an
// admin must never be sent to the launcher by a race. `null` = not known yet.
// The server enforces this (backend/app/core/workspace_access.py); the
// frontend only sends people to the page they can use.

export const WORKSPACE_ACCESS = 'access_workspace'
export const APP_USER_ROLE = 'app_user'

export function workspaceAccessOf(session: any, organizationId: string | null | undefined): boolean | null {
  const orgs = session?.organizations
  if (!Array.isArray(orgs) || !orgs.length || !organizationId) return null
  const org = orgs.find((o: any) => o.id === organizationId)
  if (!org) return null
  const permissions: string[] = Array.isArray(org.permissions) ? org.permissions : []
  if (permissions.includes('full_admin_access') || permissions.includes(WORKSPACE_ACCESS)) return true
  // An app user's resolved set is empty or holds only what a custom role
  // gave them; a server that predates the permission reports no role for it.
  return org.role !== APP_USER_ROLE && !permissions.length
}

// The pages an app user may open. Everything else redirects to /apps.
const APP_USER_PATHS = ['/apps', '/users/', '/d/', '/r/', '/c/', '/print/', '/not_found', '/embed-signin',
  '/artifact-preview/', '/i18n-smoke']

export function isAppUserPath(path: string): boolean {
  return APP_USER_PATHS.some(p => path === p || path.startsWith(p.endsWith('/') ? p : `${p}/`))
}

export const useWorkspaceAccess = () => {
  const { data } = useAuth()
  const { organization } = useOrganization()
  return computed(() => workspaceAccessOf(data.value, organization.value?.id))
}
