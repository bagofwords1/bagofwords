type UsageQuotaMetric = {
  used: number
  limit: number | null
  remaining: number | null
  percent: number | null
}

type UsageQuotaConnection = {
  id: string
  name: string
  queries: UsageQuotaMetric
  data_bytes: UsageQuotaMetric
}

// Spend is expressed in USD (floats) rather than integer counts.
type UsageQuotaSpendMetric = {
  used: number
  limit: number | null
  remaining: number | null
  percent: number | null
}

export type UsageQuotaSummary = {
  enabled: boolean
  organization_id: string
  user_id: string
  window_start?: string | null
  window_end?: string | null
  resolution_source?: string
  policy_ids?: string[]
  tokens: UsageQuotaMetric
  queries: UsageQuotaMetric
  data_bytes: UsageQuotaMetric
  spend: UsageQuotaSpendMetric
  connections: UsageQuotaConnection[]
}

export function useUsageQuota() {
  const { data: currentUser, getSession } = useAuth()
  const { organization } = useOrganization()
  const lastQuotaRefreshAt = useState<number>('usageQuota:lastRefreshAt', () => 0)

  const activeOrganization = computed(() => {
    const orgs = ((currentUser.value as any)?.organizations || []) as any[]
    return orgs.find((org: any) => org.id === organization.value.id) || null
  })

  const usageQuota = computed<UsageQuotaSummary | null>(() => {
    return (activeOrganization.value?.usage_quota || null) as UsageQuotaSummary | null
  })

  if (process.client && usageQuota.value && !lastQuotaRefreshAt.value) {
    lastQuotaRefreshAt.value = Date.now()
  }

  async function refreshQuotaIfStale(options: { force?: boolean; maxAgeMs?: number } = {}) {
    const force = options.force || false
    const maxAgeMs = options.maxAgeMs ?? 60_000
    const now = Date.now()
    const hasFreshSnapshot = usageQuota.value && lastQuotaRefreshAt.value && (now - lastQuotaRefreshAt.value) < maxAgeMs

    if (!force && hasFreshSnapshot) {
      return usageQuota.value
    }

    const session = await getSession({ force: true })
    lastQuotaRefreshAt.value = Date.now()
    const orgs = ((session as any)?.organizations || []) as any[]
    const activeOrg = orgs.find((org: any) => org.id === organization.value.id)
    return (activeOrg?.usage_quota || usageQuota.value || null) as UsageQuotaSummary | null
  }

  function markQuotaStale() {
    lastQuotaRefreshAt.value = 0
  }

  return {
    usageQuota,
    activeOrganization,
    lastQuotaRefreshAt,
    refreshQuotaIfStale,
    markQuotaStale,
  }
}
