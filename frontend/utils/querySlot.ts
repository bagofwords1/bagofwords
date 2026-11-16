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
    return t('tools.common.waitingForSlot', { connection, running: wait.running, limit: wait.limit })
  }
  return t('tools.common.waitingForSlotShort', { connection })
}
