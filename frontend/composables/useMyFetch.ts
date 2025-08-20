// /composables/useMyFetch.ts

export const useMyFetch: typeof useFetch = async (request, opts?) => {
  const config = useRuntimeConfig()
  const { token } = useAuth()
  const { organization, ensureOrganization } = useOrganization()

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
    return new Promise((resolve, reject) => {
      fetch(`${config.public.baseURL}${request}`, {
        ...opts,
        headers: opts.headers,
      }).then(response => {
        if (!response.ok) {
          reject(new Error(`HTTP error! status: ${response.status}`))
        } else {
          resolve({ data: response })
        }
      }).catch(reject)
    })
  }

  // Check if we're in a component that's already mounted by looking at the current instance
  const nuxtApp = useNuxtApp()
  const isInComponent = getCurrentInstance() !== null
  
  // Use $fetch for post-mounted requests to avoid Nuxt warning
  if (isInComponent && process.client) {
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
