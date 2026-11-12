// Connectors — each person's own accounts and tools (Gmail, Outlook, Teams,
// personal Drive, MCP presets). The backend tags every data source with
// `kind: "agent" | "connector"` (app/services/data_source_kind.py); agents are
// picked in the agent picker, connectors live in the prompt box's connectors
// menu and are used whenever the person is connected.
//
// The list comes from the shared /data_sources/active fetch (useActiveAgents),
// so the agent picker, the mention menu and the connectors menu never refetch
// the same payload.

export type ConnectorStatus = 'connected' | 'shared' | 'service_account' | 'sign_in' | 'offline'

export function isConnector(ds: any): boolean {
  return ds?.kind === 'connector'
}

// What the connector means for THIS user right now.
//   shared          — admin-configured shared access; nothing to do
//   connected       — the user's own account is linked
//   service_account — no personal link, but the admin/owner fallback applies
//   sign_in         — the user must sign in before the AI can use it
//   offline         — linked, but the last check failed (sign in again)
export function connectorStatus(ds: any): ConnectorStatus {
  const conns: any[] = ds?.connections || []
  const userReq = conns.filter((c) => c.auth_policy === 'user_required')
  if (!userReq.length && ds?.auth_policy !== 'user_required') return 'shared'
  const statuses = userReq.length ? userReq.map((c) => c.user_status || {}) : [ds?.user_status || {}]
  if (statuses.every((s) => s.has_user_credentials)) {
    return statuses.some((s) => s.connection === 'offline') ? 'offline' : 'connected'
  }
  if (statuses.every((s) => s.has_user_credentials || s.effective_auth === 'system')) return 'service_account'
  return 'sign_in'
}

export function connectorIsUsable(ds: any): boolean {
  const s = connectorStatus(ds)
  return s === 'connected' || s === 'shared' || s === 'service_account'
}

export function useConnectors() {
  const { fetchActiveAgents } = useActiveAgents()
  const toast = useToast()
  const { t } = useI18n()
  const connectors = ref<any[]>([])
  const loading = ref(false)

  async function load(opts: { force?: boolean } = {}) {
    loading.value = true
    try {
      const all = await fetchActiveAgents(opts)
      connectors.value = (all || []).filter(isConnector)
    } finally {
      loading.value = false
    }
  }

  // Sign-ins (OAuth tokens) are stored per CONNECTION, so signing out of a
  // connector removes the user's credentials on each of its personal
  // connections.
  async function signOut(ds: any): Promise<boolean> {
    const personal = (ds?.connections || []).filter((c: any) => c.auth_policy === 'user_required')
    for (const conn of personal) {
      const { error } = await useMyFetch(`/connections/${conn.id}/my-credentials`, { method: 'DELETE' })
      if (error.value) {
        toast.add({ title: t('connectors.actionFailed'), description: (error.value as any)?.data?.detail || '', color: 'red' })
        return false
      }
    }
    toast.add({ title: t('connectors.signedOut', { name: ds.name }), color: 'green' })
    await load({ force: true })
    return true
  }

  async function remove(ds: any): Promise<boolean> {
    const { error } = await useMyFetch(`/data_sources/${ds.id}`, { method: 'DELETE' })
    if (error.value) {
      toast.add({ title: t('connectors.actionFailed'), description: (error.value as any)?.data?.detail || '', color: 'red' })
      return false
    }
    toast.add({ title: t('connectors.removed', { name: ds.name }), color: 'green' })
    await load({ force: true })
    return true
  }

  return { connectors, loading, load, signOut, remove, connectorStatus, connectorIsUsable }
}
