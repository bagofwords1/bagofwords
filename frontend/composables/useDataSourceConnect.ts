// Shared "the user needs to authenticate against this agent/data source"
// detection + sign-in trigger. Wraps useConnectionSignIn so callers get a
// per-id spinner flag and a single startConnect() that either redirects
// (OAuth-only, e.g. Entra/OBO) or signals the caller to open the credentials
// modal. Keeps the Connect affordance consistent across the agents page,
// DataSourceSelector, AgentSelector and ReportAgentPanel.

export function useDataSourceConnect() {
  const signIn = useConnectionSignIn()
  const { t } = useI18n()
  const toast = useToast()

  // Data source id whose Connect button is mid-sign-in (awaiting the authorize
  // redirect). Stays set through a redirect so the spinner persists until the
  // browser unloads the page.
  const connectingId = ref<string | null>(null)

  // The first attached connection that's user_required without credentials —
  // that's what the sign-in flow should target.
  // A per-user connection needs (re)authentication when the user has no
  // credential row yet, OR the row exists but its token is expired and can't
  // refresh (needs_reconnect) — both resolve to the same Connect/Reconnect flow.
  function connectionNeedsAuth(conn: any): boolean {
    const st = conn?.user_status
    return conn?.auth_policy === 'user_required'
      && (!st?.has_user_credentials || st?.needs_reconnect === true)
  }

  function findPendingSignInConnection(ds: any): any | null {
    for (const conn of (ds?.connections || [])) {
      if (connectionNeedsAuth(conn)) {
        return conn
      }
    }
    return null
  }

  // True when the agent requires per-user auth on a connection the user hasn't
  // authenticated yet and can't fall back to system/service-account creds.
  function needsUserConnection(ds: any): boolean {
    if (!ds) return false
    const connections = ds.connections || []
    const userReqConns = connections.filter((c: any) => c.auth_policy === 'user_required')
    // Prefer connection-level status: needs connecting iff some user_required
    // connection has neither the user's own creds NOR a system fallback.
    // effective_auth === 'system' is the admin/owner service-principal fallback
    // (mirrors the /agents page) — those show as "Service account", not Connect.
    // Also avoids false positives for already-connected agents whose object
    // carries no top-level user_status (report-embedded agents only carry it
    // per-connection).
    if (userReqConns.length > 0) {
      return userReqConns.some((c: any) =>
        (!c.user_status?.has_user_credentials || c.user_status?.needs_reconnect === true)
        && c.user_status?.effective_auth !== 'system')
    }
    // No connection-level info — fall back to the agent's own status.
    if (ds.auth_policy !== 'user_required') return false
    return (ds.user_status?.has_user_credentials !== true || ds.user_status?.needs_reconnect === true)
      && ds.user_status?.effective_auth !== 'system'
  }

  // Kick off sign-in for an agent. Returns true if the caller should open the
  // credentials modal as a fallback (no OAuth-only redirect happened). On the
  // redirect path, returns false and leaves connectingId set so the button
  // keeps spinning until the browser navigates to the provider.
  async function startConnect(ds: any): Promise<boolean> {
    const pending = findPendingSignInConnection(ds)
    if (!pending) return true
    connectingId.value = ds.id
    const result = await signIn.triggerUserSignIn(pending)
    if (result.redirecting) return false
    connectingId.value = null
    if (result.error) {
      toast.add({ title: t('data.oauthStartFailed'), description: result.error, color: 'red' })
    }
    return true
  }

  // Shape an agent/data-source object for UserDataSourceCredentialsModal, which
  // resolves the connection type from a top-level `type`. Report-sourced agents
  // may only carry the type on their first connection, so backfill it.
  function asCredentialsModalSource(ds: any): any {
    if (!ds) return null
    return { ...ds, type: ds.type || ds.connections?.[0]?.type }
  }

  return {
    connectingId,
    findPendingSignInConnection,
    needsUserConnection,
    startConnect,
    asCredentialsModalSource,
  }
}
