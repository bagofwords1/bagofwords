/** A tool that has not finished: the live stream says 'running', a row
 *  loaded after a reload says 'in_progress'. */
export function isToolRunning(status?: string | null): boolean {
  return ['running', 'in_progress'].includes(status || '')
}

/** Structured outcomes win; legacy rows remain readable without changing history. */
export function toolOutcome(result: Record<string, any>, status: string, stage = '', runStatus = ''): string {
  if (result.outcome) return result.outcome
  const approval = result.approval
  if (approval?.cancelled) return 'cancelled'
  if (result.blocked_by_policy === 'ask' && runStatus === 'stopped' && approval?.timed_out) return 'cancelled'
  if (approval?.timed_out) return 'approval_expired'
  if (result.blocked_by_policy || (approval && !approval.approved)) return 'denied'
  if (['stopped', 'cancelled'].includes(status)) return 'cancelled'
  if (/remote operation timed out/i.test(result.error_message || result.error || '')) return 'execution_timed_out'
  if (result.success === false || status === 'error') return 'failed'
  if (isToolRunning(status)) {
    if (runStatus === 'stopped') return 'cancelled'
    return ['awaiting_confirmation', 'awaiting_approval'].includes(stage) ? 'awaitingApproval' : 'running'
  }
  return 'success'
}
