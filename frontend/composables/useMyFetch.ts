// /composables/useMyFetch.ts

export const useMyFetch: typeof useFetch = async (request, opts?) => {
  const config = useRuntimeConfig()
  const { token } = useAuth()
  const { organization, ensureOrganization } = useOrganization()

  // Ensure organization is loaded before making the request
  await ensureOrganization()

  opts = opts || {}
  opts.headers = {
    ...opts.headers,
    Authorization: `${token.value}`,
  }

  // Add the organization ID to the headers if it's set
  if (organization.value.id) {
    opts.headers['X-Organization-ID'] = organization.value.id
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

  return useFetch(request, { baseURL: config.public.baseURL, ...opts })
    .then(response => {
      return response
    })
    .catch(error => {
      throw error
    });
};
