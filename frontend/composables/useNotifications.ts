// Shared state + actions for the per-user notification inbox.
// The modal is mounted once in layouts/default.vue (NotificationModal.vue); this
// composable lets the sidebar bell and the modal share open-state, the item
// list, and the unread count. Backend: /api/notifications* (see InboxService).
export interface BowNotification {
  id: string
  source: string
  type: string
  severity: 'info' | 'warning' | 'error'
  title: string
  body?: string | null
  link?: string | null
  subject?: Record<string, any>
  actor_user_id?: string | null
  read: boolean
  dismissed: boolean
  created_at?: string
  updated_at?: string
}

export const useNotifications = () => {
  const isOpen = useState<boolean>('notifications-open', () => false)
  const items = useState<BowNotification[]>('notifications-items', () => [])
  const unread = useState<number>('notifications-unread', () => 0)
  const loading = useState<boolean>('notifications-loading', () => false)

  const open = () => { isOpen.value = true }
  const close = () => { isOpen.value = false }
  const toggle = () => { isOpen.value = !isOpen.value }

  const recomputeUnread = () => {
    unread.value = items.value.filter(i => !i.read && !i.dismissed).length
  }

  // Lightweight: just the badge number (polled while the app is open).
  async function fetchCount() {
    try {
      const res: any = await useMyFetch('/api/notifications/count', { method: 'GET' })
      unread.value = res?.data?.value?.unread ?? 0
    } catch { /* non-fatal */ }
  }

  // Full list for the modal.
  async function fetchItems(source?: string | null) {
    loading.value = true
    try {
      const qs = source ? `/api/notifications?source=${encodeURIComponent(source)}` : '/api/notifications'
      const res: any = await useMyFetch(qs, { method: 'GET' })
      items.value = res?.data?.value?.items ?? []
      unread.value = res?.data?.value?.unread ?? unread.value
    } catch {
      items.value = []
    } finally {
      loading.value = false
    }
  }

  async function markRead(id: string, read = true) {
    const n = items.value.find(i => i.id === id)
    if (n) n.read = read
    recomputeUnread()
    try { await useMyFetch(`/api/notifications/${id}/read`, { method: 'POST', body: { read } }) } catch {}
  }

  async function markAllRead(source?: string | null) {
    items.value.forEach(i => { if (!source || i.source === source) i.read = true })
    recomputeUnread()
    try { await useMyFetch('/api/notifications/read-all', { method: 'POST', body: { source: source ?? null } }) } catch {}
  }

  async function dismiss(id: string) {
    items.value = items.value.filter(i => i.id !== id)
    recomputeUnread()
    try { await useMyFetch(`/api/notifications/${id}/dismiss`, { method: 'POST' }) } catch {}
  }

  return {
    isOpen, items, unread, loading,
    open, close, toggle,
    fetchCount, fetchItems, markRead, markAllRead, dismiss,
  }
}
