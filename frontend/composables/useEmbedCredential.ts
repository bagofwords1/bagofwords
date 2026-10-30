// The credential of an artifact embedded in a host application (/d/{ref}?host=).
// Set by EmbeddedArtifactGate once the host has signed its user in; held in
// memory only (never storage, never a URL). While set, useMyFetch and the
// artifact runtime send it instead of the app session, so the embedded page
// acts as the host's user and nothing else.
export type EmbedCredential = {
  token: string
  organizationId: string
  hostApplication: string
  // The BOW user the session is for: renewals keep it, a different person changes it.
  userId?: string | null
  // ARTIFACT_DATA.current_user for the embedded artifact (viewer_context shape).
  viewer: Record<string, any> | null
}

export const useEmbedCredential = () => useState<EmbedCredential | null>('bow-embed-credential', () => null)

// The Authorization header value for API calls from this page.
export const useApiAuthorization = () => {
  const { token } = useAuth()
  const embed = useEmbedCredential()
  return computed(() => (embed.value ? `Bearer ${embed.value.token}` : (token.value || '')))
}
