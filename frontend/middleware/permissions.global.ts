import { useCan, useCanAny, usePermissionsLoaded } from '~/composables/usePermissions'

type ResourcePermissionAny = { permission: string; resourceType: string }

export default defineNuxtRouteMiddleware(async (to, from) => {
  // Skip permission checks for auth/public pages
  const publicPaths = ['/users/', '/organizations/', '/onboarding', '/r/', '/not_found']
  if (publicPaths.some(path => to.path.startsWith(path))) {
    return
  }

  // Two ways a route can declare its access requirements:
  //   meta.permissions: ['org_perm']               → must hold the org perm
  //   meta.resourcePermissionAny: {permission, resourceType}
  //                                                → must hold this perm on
  //                                                  ANY resource of the given
  //                                                  type (e.g. manage on
  //                                                  any data_source). Use this
  //                                                  for pages that already
  //                                                  filter their data per
  //                                                  resource on the backend.
  const requiredPermissions = (to.meta.permissions as string[] | undefined) || []
  const resourceAny = to.meta.resourcePermissionAny as ResourcePermissionAny | undefined
  if (!requiredPermissions.length && !resourceAny) {
    return
  }

  // Get auth status - if not authenticated, let the auth middleware handle redirect
  const { status } = useAuth()
  if (status.value !== 'authenticated') {
    return
  }

  // Check if permissions have been loaded
  const permissionsLoaded = usePermissionsLoaded()

  // If permissions haven't loaded yet, don't block - let the page load
  // The permissions plugin will handle loading them
  if (!permissionsLoaded.value) {
    return
  }

  // Check if user has all required permissions
  let hasPermission = true
  for (const permission of requiredPermissions) {
    const can = useCan(permission)
    if (!can) {
      hasPermission = false
      break
    }
  }
  if (hasPermission && resourceAny) {
    if (!useCanAny(resourceAny.permission, resourceAny.resourceType)) {
      hasPermission = false
    }
  }

  if (!hasPermission) {
    // Don't redirect to '/' if already on '/' to avoid infinite loop
    if (to.path === '/' || to.path === '') {
      return
    }
    
    // Redirect to home for protected pages user can't access
    return navigateTo('/')
  }
})
