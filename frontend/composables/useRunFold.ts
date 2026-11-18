/**
 * Run-fold view state shared by the report page and the shared link.
 *
 * `computeRunActivity` (useRunActivity.ts) decides how a finished run folds;
 * this module holds what both transcripts do with that decision: which block
 * renders while the steps are closed, which toggles are open, and a per-message
 * memo so a streamed token does not recompute every run in the conversation.
 */
import { ref } from 'vue'
import { computeRunActivity, type RunActivity } from './useRunActivity'

type FoldOptions = Parameters<typeof computeRunActivity>[1]

/**
 * `computeRunActivity`, memoized per message. The key covers everything the
 * fold reads from a block (id, statuses, whether it has prose, the planner's
 * completion flag) plus any page-specific `extraKey` (steering boundaries,
 * superseded blocks), so a token that only lengthens an answer reuses the fold.
 */
export function createRunActivityMemo() {
  const cache = new Map<string, { key: string; activity: RunActivity | null }>()
  const hasText = (v: any) => typeof v === 'string' && v.trim() !== ''
  const blockKey = (b: any) => {
    const pd = b?.plan_decision
    const prose = hasText(b?.content) || hasText(pd?.final_answer) || hasText(pd?.assistant)
    // While live, the row's status line reads the progress notes and titles.
    const live = `${(b?.content || pd?.assistant || '').length}:${b?.tool_execution?.tool_name ?? ''}:${b?.tool_execution?.arguments_json?.title ?? ''}:${b?.tool_execution?.created_step?.title ?? ''}:${b?.tool_execution?.progress_stage ?? ''}`
    return `${b?.id}:${b?.status}:${b?.tool_execution?.status ?? ''}:${prose ? 1 : 0}:${pd?.analysis_complete ? 1 : 0}:${live}`
  }
  return function runActivityFor(
    messageId: string,
    blocks: any[],
    opts: FoldOptions & { extraKey?: string } = {},
  ): RunActivity | null {
    const key = `${opts?.runStatus}|${opts?.extraKey ?? ''}|${(blocks || []).map(blockKey).join(',')}`
    const hit = cache.get(messageId)
    if (hit && hit.key === key) return hit.activity
    const activity = computeRunActivity(blocks, opts)
    cache.set(messageId, { key, activity })
    return activity
  }
}

/**
 * The fold's view helpers. `foldFor` returns the message's RunActivity (or
 * null), `blocksFor` the blocks the transcript renders, and `keyFor` the key
 * the open/closed state survives under.
 */
export function useRunFold<M>(opts: {
  foldFor: (m: M) => RunActivity | null | undefined
  blocksFor: (m: M) => any[]
  keyFor: (m: M) => string
}) {
  const expandedRuns = ref<Set<string>>(new Set())
  const collapsedRuns = ref<Set<string>>(new Set())
  const expandedToolGroups = ref<Set<string>>(new Set())

  const runFold = (m: M): RunActivity | undefined => {
    const activity = opts.foldFor(m)
    // A finished text-only answer has no activity control to reopen its blocks.
    return activity && (activity.live || activity.steps.length) ? activity : undefined
  }
  const isRunExpanded = (m: M): boolean => expandedRuns.value.has(opts.keyFor(m)) || (!!runFold(m)?.live && !collapsedRuns.value.has(opts.keyFor(m)))
  function toggleRun(m: M) {
    const next = new Set(expandedRuns.value)
    const key = opts.keyFor(m)
    const closing = isRunExpanded(m)
    closing ? next.delete(key) : next.add(key)
    const closed = new Set(collapsedRuns.value)
    closing ? closed.add(key) : closed.delete(key)
    collapsedRuns.value = closed
    expandedRuns.value = next
  }
  // Part of the answer shown while the steps are closed.
  const isRunAnswerBlock = (m: M, block: any): boolean => !!runFold(m)?.answerIds.includes(String(block.id))
  // Shown in full while folded: a card waiting on the user.
  const isRunOpenBlock = (m: M, block: any): boolean => !!runFold(m)?.openIds.includes(String(block.id))
  // The answer always renders; the activity before it only while open.
  function isRunBlockShown(m: M, block: any): boolean {
    return !runFold(m) || isRunExpanded(m) || isRunAnswerBlock(m, block) || isRunOpenBlock(m, block)
  }
  // Activity preceding the answer: everything before the answer while the steps
  // are open.
  function isRunActivityBlock(m: M, block: any): boolean {
    return !!runFold(m) && !isRunAnswerBlock(m, block) && (isRunExpanded(m) || isRunOpenBlock(m, block))
  }
  const toolGroupFor = (m: M, block: any) => runFold(m)?.toolGroups.find(g => g.blockIds.includes(String(block.id)))
  const toolGroupHeaderFor = (m: M, block: any) => {
    const group = toolGroupFor(m, block)
    return isRunExpanded(m) && group && group.blockIds.length > 1 && group.id === String(block.id) ? group : undefined
  }
  const isGroupedTool = (m: M, block: any): boolean => (toolGroupFor(m, block)?.blockIds.length || 0) > 1
  const isToolGroupExpanded = (id: string) => expandedToolGroups.value.has(id)
  function toggleToolGroup(id: string) {
    const next = new Set(expandedToolGroups.value)
    next.has(id) ? next.delete(id) : next.add(id)
    expandedToolGroups.value = next
  }
  // A group opens individual tool rows, each retaining its own collapsed state.
  function isRunReasoningShown(m: M, block: any): boolean {
    const fold = runFold(m)
    if (!fold) return true
    if (fold.live && !fold.steps.length) return false
    const group = toolGroupFor(m, block)
    return isRunExpanded(m) && (!group || isToolGroupExpanded(group.id))
  }
  function isRunToolShown(m: M, block: any): boolean {
    const group = toolGroupFor(m, block)
    return !runFold(m) || isRunOpenBlock(m, block) || (isRunExpanded(m) && (!group || group.blockIds.length === 1 || isToolGroupExpanded(group.id)))
  }
  function runDeliverables(m: M): any[] {
    const fold = runFold(m)
    if (!fold?.deliverableIds.length) return []
    const byId = new Map(opts.blocksFor(m).map((b: any) => [String(b.id), b]))
    return fold.deliverableIds.map((id) => byId.get(id)).filter(Boolean)
  }

  return {
    expandedRuns,
    toolGroupHeaderFor,
    isGroupedTool,
    isToolGroupExpanded,
    toggleToolGroup,
    runFold,
    isRunExpanded,
    toggleRun,
    isRunAnswerBlock,
    isRunOpenBlock,
    isRunBlockShown,
    isRunActivityBlock,
    isRunReasoningShown,
    isRunToolShown,
    runDeliverables,
  }
}
