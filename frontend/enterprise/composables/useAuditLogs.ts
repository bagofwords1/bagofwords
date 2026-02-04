// Audit Logs Composable
// Licensed under the Business Source License 1.1

export type AuditLog = {
  id: string
  organization_id: string
  user_id: string | null
  user_email: string | null
  action: string
  resource_type: string | null
  resource_id: string | null
  details: Record<string, any> | null
  ip_address: string | null
  user_agent: string | null
  created_at: string
}

export type AuditLogFilters = {
  action?: string
  resource_type?: string
  user_id?: string
  start_date?: string
  end_date?: string
  search?: string
}

export type AuditLogsResponse = {
  items: AuditLog[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

export const useAuditLogs = () => {
  const logs = ref<AuditLog[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)
  const total = ref(0)
  const page = ref(1)
  const pageSize = ref(50)
  const totalPages = ref(0)

  const fetchLogs = async (filters?: AuditLogFilters) => {
    loading.value = true
    error.value = null

    try {
      const params = new URLSearchParams()
      params.append('page', page.value.toString())
      params.append('page_size', pageSize.value.toString())

      if (filters?.action) params.append('action', filters.action)
      if (filters?.resource_type) params.append('resource_type', filters.resource_type)
      if (filters?.user_id) params.append('user_id', filters.user_id)
      if (filters?.start_date) params.append('start_date', filters.start_date)
      if (filters?.end_date) params.append('end_date', filters.end_date)
      if (filters?.search) params.append('search', filters.search)

      const res = await useMyFetch(`/api/enterprise/audit?${params.toString()}`)

      if (res.status.value !== 'success') {
        const msg = (res.error?.value as any)?.data?.detail || 'Failed to fetch audit logs'
        throw new Error(msg)
      }

      const data = res.data.value as AuditLogsResponse
      logs.value = data.items
      total.value = data.total
      totalPages.value = data.total_pages
    } catch (e: any) {
      error.value = e.message || 'Failed to fetch audit logs'
      logs.value = []
    } finally {
      loading.value = false
    }
  }

  const nextPage = async (filters?: AuditLogFilters) => {
    if (page.value < totalPages.value) {
      page.value++
      await fetchLogs(filters)
    }
  }

  const prevPage = async (filters?: AuditLogFilters) => {
    if (page.value > 1) {
      page.value--
      await fetchLogs(filters)
    }
  }

  const goToPage = async (newPage: number, filters?: AuditLogFilters) => {
    if (newPage >= 1 && newPage <= totalPages.value) {
      page.value = newPage
      await fetchLogs(filters)
    }
  }

  return {
    logs,
    loading,
    error,
    total,
    page,
    pageSize,
    totalPages,
    fetchLogs,
    nextPage,
    prevPage,
    goToPage,
  }
}
