// A query waiting for a free slot on its connection: the connection is already
// running its maximum number of concurrent queries (an app-wide limit, set per
// connection), and this one waits its turn instead of failing.
export interface SlotWait {
  connection_name?: string | null
  running?: number | null
  limit?: number | null
  queued_ahead?: number | null
}

type Translate = (key: string, params?: Record<string, unknown>) => string

export function slotWaitText(t: Translate, wait: SlotWait | null | undefined): string {
  if (!wait) return ''
  const connection = wait.connection_name || t('tools.common.theConnection')
  if (typeof wait.running === 'number' && typeof wait.limit === 'number') {
    // A query also waits behind earlier queued ones, so "1/2 running" alone
    // would read as a free slot; name the queue ahead of it.
    if (typeof wait.queued_ahead === 'number' && wait.queued_ahead > 0) {
      return t('tools.common.waitingForSlotAhead', {
        connection, running: wait.running, limit: wait.limit, ahead: wait.queued_ahead,
      })
    }
    return t('tools.common.waitingForSlot', { connection, running: wait.running, limit: wait.limit })
  }
  return t('tools.common.waitingForSlotShort', { connection })
}
