// /composables/useMyFetch.ts

// Guards against a redirect storm: when a stale token is rejected, every
// in-flight API call gets a 401 at once, but we only want a single redirect.
let redirectingToSignIn = false

export const useMyFetch: typeof useFetch = async (request, opts?) => {
  const config = useRuntimeConfig()
  const { token } = useAuth()
  const { rawToken } = useAuthState()
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
    redirectingToSignIn = true
    // Clear the rejected token, then HARD-redirect. A SPA navigateTo gets
    // bounced right back by the sign-in page's `unauthenticatedOnly` guard while
    // nuxt-auth still derives status from the (now invalid) token, producing a
    // loop. A full reload re-evaluates auth from the cleared cookie and lands on
    // sign-in cleanly. No flag reset needed — the page is reloading.
    rawToken.value = null
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
