/**
 * Run-fold view state shared by the report page and the shared link.
 *
 * `computeRunActivity` (useRunActivity.ts) decides how a finished run folds;
 * this module holds what both transcripts do with that decision: which block
 * renders while the steps are closed, which toggles are open, and a per-message
 * memo so a streamed token does not recompute every run in the conversation.
 */
import { ref } from 'vue'
import { computeRunActivity, type RunActivity, type RunStep } from './useRunActivity'

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
    return `${b?.id}:${b?.status}:${b?.tool_execution?.status ?? ''}:${prose ? 1 : 0}:${pd?.analysis_complete ? 1 : 0}`
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
  const expandedRunSteps = ref<Set<string>>(new Set())

  const runFold = (m: M): RunActivity | undefined => opts.foldFor(m) || undefined
  const isRunExpanded = (m: M): boolean => expandedRuns.value.has(opts.keyFor(m))
  function toggleRun(m: M) {
    const next = new Set(expandedRuns.value)
    const key = opts.keyFor(m)
    next.has(key) ? next.delete(key) : next.add(key)
    expandedRuns.value = next
  }
  const isRunStepExpanded = (stepId: string): boolean => expandedRunSteps.value.has(stepId)
  function toggleRunStep(stepId: string) {
    const next = new Set(expandedRunSteps.value)
    next.has(stepId) ? next.delete(stepId) : next.add(stepId)
    expandedRunSteps.value = next
  }

  // A create_data call that produced a chart: never folded, card kept in place.
  const isRunInlineBlock = (m: M, block: any): boolean => !!runFold(m)?.inlineIds.includes(String(block.id))
  // Part of the answer shown while the steps are closed.
  const isRunAnswerBlock = (m: M, block: any): boolean => !!runFold(m)?.answerIds.includes(String(block.id))
  // The answer and inline charts always render; the rest only while open.
  function isRunBlockShown(m: M, block: any): boolean {
    return !runFold(m) || isRunExpanded(m) || isRunAnswerBlock(m, block) || isRunInlineBlock(m, block)
  }
  // Part of the indented thread: everything before the answer while the steps
  // are open. A chart shown while they are closed stands on its own.
  function isRunActivityBlock(m: M, block: any): boolean {
    return !!runFold(m) && !isRunAnswerBlock(m, block) && isRunExpanded(m)
  }
  // Progress prose belongs to the chain: with the steps closed, an inline chart
  // shows its card only, not the sentence that introduced it.
  function isRunProseShown(m: M, block: any): boolean {
    return !runFold(m) || isRunExpanded(m) || isRunAnswerBlock(m, block)
  }
  function runStepHeaderFor(m: M, block: any): RunStep | undefined {
    const fold = runFold(m)
    return fold && isRunExpanded(m) ? fold.headerAt[String(block.id)] : undefined
  }
  // Later members of a step keep their reasoning and call in the step details.
  function isRunStepDetail(m: M, block: any): boolean {
    const step = runFold(m)?.stepOf[String(block.id)]
    return !!step && step.id !== String(block.id)
  }
  // A step's first member shows its reasoning in the chain (it precedes the
  // step); later members' reasoning opens with the step.
  function isRunReasoningShown(m: M, block: any): boolean {
    const fold = runFold(m)
    if (!fold) return true
    if (!isRunExpanded(m)) return false
    const step = fold.stepOf[String(block.id)]
    return !step || step.id === String(block.id) || isRunStepExpanded(step.id)
  }
  function isRunToolShown(m: M, block: any): boolean {
    const fold = runFold(m)
    if (!fold || isRunInlineBlock(m, block)) return true
    const step = fold.stepOf[String(block.id)]
    return isRunExpanded(m) && !!step && isRunStepExpanded(step.id)
  }
  function runDeliverables(m: M): any[] {
    const fold = runFold(m)
    if (!fold?.deliverableIds.length) return []
    const byId = new Map(opts.blocksFor(m).map((b: any) => [String(b.id), b]))
    return fold.deliverableIds.map((id) => byId.get(id)).filter(Boolean)
  }

  return {
    expandedRuns,
    expandedRunSteps,
    runFold,
    isRunExpanded,
    toggleRun,
    isRunStepExpanded,
    toggleRunStep,
    isRunInlineBlock,
    isRunAnswerBlock,
    isRunBlockShown,
    isRunActivityBlock,
    isRunProseShown,
    runStepHeaderFor,
    isRunStepDetail,
    isRunReasoningShown,
    isRunToolShown,
    runDeliverables,
  }
}
