// `status === 'waiting'` covers two states on a sub-agent row: blocked on the
// parent's answer, or answered and queued for a free slot (`waiting_for`).
// Every surface that labels the status goes through this so neither reads as
// "still needs an answer" once the parent has replied.
export function subAgentStatusKey(row: any): string {
  const status = String(row?.status || '')
  return status === 'waiting' && row?.waiting_for === 'slot' ? 'waiting_slot' : status
}
