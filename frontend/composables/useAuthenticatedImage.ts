/**
 * Composable to fetch images with authentication and return blob URLs.
 * Needed because <img src="/api/..."> doesn't include the Bearer token.
 */
const imageCache = new Map<string, string>()

export function useAuthenticatedImage() {
  const { token } = useAuth()
  const { organization } = useOrganization()

  async function getImageUrl(fileId: string): Promise<string> {
    // Return cached URL if available
    if (imageCache.has(fileId)) {
      return imageCache.get(fileId)!
    }

    try {
      const headers: Record<string, string> = {
        'Authorization': `${token.value}`  // token already includes "Bearer "
      }

      if (organization.value?.id) {
        headers['X-Organization-Id'] = organization.value.id
      }

      const response = await fetch(`/api/files/${fileId}/content`, { headers })

      if (!response.ok) {
        console.error('Failed to fetch image:', response.status)
        return ''
      }

      const blob = await response.blob()
      const blobUrl = URL.createObjectURL(blob)
      imageCache.set(fileId, blobUrl)
      return blobUrl
    } catch (error) {
      console.error('Error fetching image:', error)
      return ''
    }
  }

  return {
    getImageUrl
  }
}
