export default defineNuxtRouteMiddleware(async (to, from) => {
  const { status, getSession } = useAuth()
  const session = await getSession()

  // If user is authenticated but not verified, redirect to verify page
  if (status.value === 'authenticated' && !session.is_verified) {
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

  