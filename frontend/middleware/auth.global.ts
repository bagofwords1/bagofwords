export default defineNuxtRouteMiddleware(async (to, from) => {
  const { status, getSession } = useAuth()
  const session = await getSession()

  // Only enforce verification once we actually have session data. A stale or
  // expired token can leave status 'authenticated' while the session is still
  // null/undefined (the /whoami it resolves to returns 401); touching
  // session.is_verified in that state threw and crashed every route, including
  // the sign-in page itself. Guarding on `session` keeps the login page usable.
  if (status.value === 'authenticated' && session) {
    // If user is authenticated but not verified, redirect to verify page
    if (!session.is_verified) {
      if (!to.path.startsWith('/users/verify')) {
        return navigateTo('/users/verify')
      }
    } else if (to.path === '/users/verify') {
      // If user is verified but still on verify page, redirect to home
      return navigateTo('/', { replace: true })
    }
  }
})

  