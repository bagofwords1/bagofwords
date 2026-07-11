// Create a public, org-wide agent linked to a freshly-saved connection, with
// per-user (OBO) auth so each user signs in individually before using it.
//
// Shared by the connector forms (Integration, MCP, …) behind the
// "Create a public agent with this <connection>" toggle. Best-effort: the
// caller decides how to surface success/failure (a duplicate name, etc.).
export async function createPublicAgent(
  connectionId: string,
  opts: { name: string; type: string },
) {
  return useMyFetch('/data_sources', {
    method: 'POST',
    body: {
      name: opts.name.trim(),
      type: opts.type,
      connection_ids: [connectionId],
      // Org-wide: the toggle promises "everyone in your org can use it".
      is_public: true,
      // OBO: no shared credential — each user authenticates for themselves.
      auth_policy: 'user_required',
      allowed_user_auth_modes: ['oauth'],
    },
  })
}
