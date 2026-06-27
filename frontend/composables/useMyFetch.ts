// /composables/useMyFetch.ts

// Guards against a redirect storm: when a stale token is rejected, every
// in-flight API call gets a 401 at once, but we only want a single redirect.
let redirectingToSignIn = false

// Circuit breaker against a hard-reload loop: if clearing the cookie ever fails
// to take, a blind window.location reload would spin forever. We record the last
// forced sign-out and refuse to do another within this window; a successful
// request clears it so a genuine later expiry still redirects.
const FORCED_SIGNOUT_KEY = 'bow:forcedSignOutAt'
const FORCED_SIGNOUT_COOLDOWN_MS = 15000

export const useMyFetch: typeof useFetch = async (request, opts?) => {
  const config = useRuntimeConfig()
  const { token } = useAuth()
  const { rawToken, data: authData } = useAuthState()
  const { organization, ensureOrganization } = useOrganization()
  const route = useRoute()

  const isClient = process.client

  // The server rejected our token (expired, revoked, or its signing secret
  // changed). nuxt-auth still reports 'authenticated' from the token's mere
  // presence, so without this the user is stranded on an empty app shell with
  // every request 401ing. Clear the stale session and send them to sign-in.
  const handleUnauthenticated = () => {
    if (!isClient || redirectingToSignIn) return
    // Don't loop on the auth/public pages that legitimately run unauthenticated.
    const publicPrefixes = ['/users/', '/organizations/', '/onboarding', '/r/', '/c/', '/not_found']
    if (publicPrefixes.some(p => route.path.startsWith(p))) return
    // Circuit breaker: if we already forced a sign-out very recently and are
    // STILL getting 401s on a protected page, the cookie clear didn't take —
    // stop reloading so the user isn't trapped in a refresh loop.
    try {
      const last = Number(sessionStorage.getItem(FORCED_SIGNOUT_KEY) || 0)
      if (last && Date.now() - last < FORCED_SIGNOUT_COOLDOWN_MS) return
      sessionStorage.setItem(FORCED_SIGNOUT_KEY, String(Date.now()))
    } catch {}
    redirectingToSignIn = true
    // Clear every trace of the rejected session, then HARD-redirect. rawToken
    // alone is not enough: this app pins @sidebase/nuxt-auth 0.9.3, which does
    // not honor the nested token.cookie config in nuxt.config, so the token
    // actually lives in the default `auth.token` cookie. If that cookie
    // survives, nuxt-auth keeps reporting status 'authenticated', the sign-in
    // page's `unauthenticatedOnly` guard bounces back to /, and every API call
    // 401s again — an infinite loop. So drop the cached session data and
    // hard-delete the cookie by name before a full reload, which re-evaluates
    // auth from the now-absent cookie and lands on sign-in cleanly.
    rawToken.value = null
    if (authData) authData.value = null
    for (const name of ['auth.token', 'auth_token']) {
      document.cookie = `${name}=; Max-Age=0; path=/`
    }
    const redirect = route.fullPath && route.fullPath !== '/'
      ? `?redirect=${encodeURIComponent(route.fullPath)}`
      : ''
    window.location.href = `/users/sign-in${redirect}`
  }

  // Ensure organization is loaded before making the request
  const orgResult = await ensureOrganization()

  opts = opts || {}
  opts.headers = {
    ...opts.headers,
    Authorization: `${token.value}`,
  }

  // Add the organization ID to the headers if it's set
  // Use the returned organization from ensureOrganization to avoid timing issues
  if (orgResult?.id) {
    opts.headers['X-Organization-Id'] = orgResult.id
  } else {
    // Still make the request but without org header - let backend handle the error
    console.warn('No organization ID available for API request:', request)
  }

  if (opts.stream) {
    const { stream: _, headers: rawHeaders, ...fetchOpts } = opts as any
    const headers = { ...(rawHeaders as Record<string, string>), 'Accept': 'text/event-stream', 'Cache-Control': 'no-cache' }
    return new Promise((resolve, reject) => {
      fetch(`${config.public.baseURL}${request}`, {
        ...fetchOpts,
        headers,
      }).then(response => {
        if (!response.ok) {
          reject(new Error(`HTTP error! status: ${response.status}`))
        } else {
          resolve({ data: response })
        }
      }).catch(reject)
    })
  }

  // This app is a client-side SPA. On the client, prefer $fetch so calls made
  // from onMounted/watch/event handlers do not register new async-data entries
  // during route transitions.
  if (isClient) {
    try {
      const data = await $fetch(request, {
        baseURL: config.public.baseURL,
        ...opts
      })
      // A successful authenticated request means the session is healthy again;
      // reset the forced-sign-out breaker so a future expiry can redirect.
      try { sessionStorage.removeItem(FORCED_SIGNOUT_KEY) } catch {}
      return {
        data: ref(data),
        error: ref(null),
        pending: ref(false),
        refresh: () => {},
        status: ref('success')
      }
    } catch (error: any) {
      if (error?.status === 401 || error?.statusCode === 401 || error?.response?.status === 401) {
        handleUnauthenticated()
      }
      return {
        data: ref(null),
        error: ref(error),
        pending: ref(false),
        refresh: () => {},
        status: ref('error')
      }
    }
  }

  return useFetch(request, { baseURL: config.public.baseURL, ...opts })
    .then(response => {
      return response
    })
    .catch(error => {
      throw error
    });
};
