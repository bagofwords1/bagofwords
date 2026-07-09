// /composables/useMyFetch.ts

export const useMyFetch: typeof useFetch = async (request, opts?) => {
  const config = useRuntimeConfig()
  const { token } = useAuth()
  const { organization, ensureOrganization } = useOrganization()

  const isClient = process.client

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
    } catch (error) {
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

// Strict variant for MUTATING calls (POST/PUT/PATCH/DELETE handlers).
//
// `useMyFetch` follows the `useFetch` contract: it NEVER rejects on an
// HTTP-level failure — the error lands in the returned `error` ref and the
// promise resolves. Any `try/catch` around a bare `await useMyFetch(...)`
// is therefore dead code for 4xx/5xx responses (issue #584: handlers mutated
// state and showed success toasts on failed backend calls).
//
// `useMyFetchStrict` keeps the exact same auth/org/baseURL behavior and the
// same success-value shape, but THROWS the original fetch error on HTTP
// failure, so `try { await useMyFetchStrict(...) } catch (e) { ... }` works
// the way callers expect. Pair it with `useErrorMessage().getErrorMessage(e)`
// in the catch to render the failure.
export const useMyFetchStrict: typeof useFetch = async (request, opts?) => {
  const response: any = await useMyFetch(request, opts as any)
  if (response?.error?.value) {
    throw response.error.value
  }
  return response
}
