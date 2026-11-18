export default defineNuxtRouteMiddleware(async (to, from) => {
  if (to.path.startsWith('/artifact-preview/')) return // API remains authenticated by the preview broker.
  const { status, getSession, data } = useAuth()
  // Reuse the session already loaded by the permissions plugin / sidebase's own
  // middleware instead of firing a fresh whoami on every navigation. Only hit
  // the network when there is genuinely no session in the store yet. The session
  // refreshes on window focus and the verify flow does a full reload, so
  // is_verified stays fresh without a per-navigation round-trip.
  const session = (data?.value as any) || (await getSession())

  // An invite link opened by someone who is already signed in: accept it for
  // this account instead of bouncing off the (guests-only) sign-up page. The
  // backend only accepts a link that was issued to this account's email.
  const inviteToken = to.path === '/users/sign-up' ? to.query.token : null
  if (process.client && status.value === 'authenticated' && typeof inviteToken === 'string' && inviteToken) {
    try {
      const { token } = useAuth()
      const accepted: any = await $fetch('/api/invites/accept', {
        method: 'POST',
        headers: { Authorization: `${token.value}` },
        body: { token: inviteToken },
      })
      if (accepted?.organization_id) {
        try { localStorage.setItem('bow.selectedOrganizationId', accepted.organization_id) } catch {}
        // Full reload so the session and all org-scoped state are rebuilt.
        window.location.href = '/'
        return abortNavigation()
      }
    } catch (_) {
      // Not this account's invite (or expired): fall through to the normal redirect.
    }
  }

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


