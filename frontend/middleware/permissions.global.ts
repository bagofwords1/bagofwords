import { useCan, usePermissionsLoaded } from '~/composables/usePermissions'

export default defineNuxtRouteMiddleware(async (to, from) => {
  // Skip permission checks for auth/public pages
  const publicPaths = ['/users/', '/organizations/', '/onboarding', '/r/', '/not_found']
  if (publicPaths.some(path => to.path.startsWith(path))) {
    return
  }

  // Skip if no permissions required for this route
  const requiredPermissions = (to.meta.permissions as string[] | undefined) || []
  if (!requiredPermissions.length) {
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

  if (!hasPermission) {
    // Don't redirect to '/' if already on '/' to avoid infinite loop
    if (to.path === '/' || to.path === '') {
      return
    }
    
    // Redirect to home for protected pages user can't access
    return navigateTo('/')
  }
})
