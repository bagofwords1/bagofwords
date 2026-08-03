// /composables/useMyFetch.ts

// Concurrent identical GETs share one network call. Different components
// routinely request the same endpoint within milliseconds of each other on
// page load (measured: organization/members twice at a 0ms gap, review-hunks
// twice 97ms apart, whoami/settings/llm-models each twice during report boot)
// and every duplicate response triggered its own state update and re-render —
// the visible "flicker" while a page settles. In-flight-only, never a TTL
// cache: once a response lands the entry is gone, so nothing is ever served
// stale. Each caller gets its own clone of the body so one component mutating
// its copy (sorting in place, etc.) cannot alias another's.
const inflightGets = new Map<string, Promise<any>>()

const cloneBody = (data: any) => {
  try { return structuredClone(data) } catch { return data }
}

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
      const method = String((opts as any).method || 'GET').toUpperCase()
      const canDedupe =
        method === 'GET' && typeof request === 'string' && !(opts as any).body
      let data: any
      if (canDedupe) {
        const key = `${orgResult?.id || ''}|${request}|` +
          JSON.stringify((opts as any).query ?? (opts as any).params ?? null)
        let inflight = inflightGets.get(key)
        if (!inflight) {
          inflight = $fetch(request, { baseURL: config.public.baseURL, ...opts })
            .finally(() => { inflightGets.delete(key) })
          inflightGets.set(key, inflight)
        }
        data = cloneBody(await inflight)
      } else {
        data = await $fetch(request, {
          baseURL: config.public.baseURL,
          ...opts
        })
      }
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
