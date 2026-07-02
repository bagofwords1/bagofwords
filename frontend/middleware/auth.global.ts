export default defineNuxtRouteMiddleware(async (to, from) => {
  const { status, getSession, data } = useAuth()
  // Reuse the session already loaded by the permissions plugin / sidebase's own
  // middleware instead of firing a fresh whoami on every navigation. Only hit
  // the network when there is genuinely no session in the store yet. The session
  // refreshes on window focus and the verify flow does a full reload, so
  // is_verified stays fresh without a per-navigation round-trip.
  const session = (data?.value as any) || (await getSession())

  // If user is authenticated but not verified, redirect to verify page
  if (status.value === 'authenticated' && session && !session.is_verified) {
    if (!to.path.startsWith('/users/verify')) {
      return navigateTo('/users/verify')
    }
  }

  // If user is verified but still on verify page, redirect to home
  if (status.value === 'authenticated' && session?.is_verified && to.path === '/users/verify') {
    console.log('Redirecting verified user from verify page to home')
    return navigateTo('/', { replace: true })
  }

})

  