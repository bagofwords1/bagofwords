// Invite token of a signed-out visitor, kept for this tab until they sign in.
const PENDING_INVITE_KEY = 'bow.pendingInviteToken'

export default defineNuxtRouteMiddleware(async (to, from) => {
  if (to.path.startsWith('/artifact-preview/')) return // API remains authenticated by the preview broker.
  const { status, getSession, data } = useAuth()
  // Reuse the session already loaded by the permissions plugin / sidebase's own
  // middleware instead of firing a fresh whoami on every navigation. Only hit
  // the network when there is genuinely no session in the store yet. The session
  // refreshes on window focus and the verify flow does a full reload, so
  // is_verified stays fresh without a per-navigation round-trip.
  const session = (data?.value as any) || (await getSession())

  // An invite link for someone who already has an account. Opened while
  // signed in, it is accepted for this account right away. Opened while
  // signed out, the token is remembered for this tab (sessionStorage survives
  // the password form and the SSO round trip alike, and is never sent
  // anywhere but our own API) and accepted on the first navigation after
  // sign-in. The backend only accepts a link issued to this account's email;
  // any refusal (someone else's link, expired, already used by registration)
  // is dropped silently and navigation continues as usual.
  if (process.client) {
    const fromUrl = to.path === '/users/sign-up' ? to.query.token
      : to.path === '/users/sign-in' ? to.query.invite_token : null
    const linkToken = typeof fromUrl === 'string' && fromUrl ? fromUrl : null
    if (linkToken) {
      try { sessionStorage.setItem(PENDING_INVITE_KEY, linkToken) } catch {}
    }
    if (status.value === 'authenticated') {
      let pending: string | null = linkToken
      try { pending = sessionStorage.getItem(PENDING_INVITE_KEY) || pending } catch {}
      if (pending) {
        try { sessionStorage.removeItem(PENDING_INVITE_KEY) } catch {}
        try {
          const { token } = useAuth()
          const accepted: any = await $fetch('/api/invites/accept', {
            method: 'POST',
            headers: { Authorization: `${token.value}` },
            body: { token: pending },
          })
          if (accepted?.organization_id) {
            try { localStorage.setItem('bow.selectedOrganizationId', accepted.organization_id) } catch {}
            // Full reload so the session and all org-scoped state are rebuilt
            // in the organization that was just joined.
            window.location.href = '/'
            return abortNavigation()
          }
        } catch (_) {}
      }
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


