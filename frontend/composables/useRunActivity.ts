/**
 * Run-level activity fold for the report conversation.
 *
 * While a run works, its blocks render chronologically as they arrive. Once
 * the run's FINAL MESSAGE has arrived, everything that came before it folds
 * into one "Worked for 10s · View steps" row above the conclusion. Expanding
 * the row shows the chain again, with consecutive same-purpose tool calls
 * merged under one plain-language step ("Checked the customers table").
 *
 * Pure functions — no Vue reactivity — so the policy is unit-testable
 * (tests/unit/useRunActivity.mjs). Labels are returned as i18n keys + params;
 * the page translates them in the response's language.
 *
 * Policy:
 * - The final message is the LAST block, when it carries user-facing prose and
 *   the run is known to be over (the planner marked it `analysis_complete`, or
 *   the run's status is `success`). A run that stopped, errored, or ended on a
 *   question (clarify) has no conclusion and never folds: its failure or its
 *   question must stay in view.
 * - Prose always breaks a step: a progress message is part of the chain and
 *   keeps its chronological place. Reasoning does not — it rides inside the
 *   step it preceded.
 * - A `create_data` call is always its own step, described by what it produced.
 * - Outputs the user would open or act on (tables, charts, dashboards, docs,
 *   review cards…) are returned as `deliverableIds` so the page can keep them
 *   reachable below the final message while the steps are collapsed.
 */
// Kept local rather than imported from useBlockGrouping: both modules stay
// dependency-free so `node tests/unit/*.mjs` can load them without a bundler.
// The two predicates mirror isBlockFailed / blockHasMessage there.
const FAILED_STATUSES = new Set(['error', 'failed', 'stopped'])

function isBlockFailed(block: any): boolean {
  return FAILED_STATUSES.has(String(block?.tool_execution?.status || ''))
    || FAILED_STATUSES.has(String(block?.status || ''))
}

function blockHasMessage(block: any): boolean {
  const pd = block?.plan_decision
  const hasText = (v: any) => typeof v === 'string' && v.trim() !== ''
  return hasText(block?.content) || hasText(pd?.final_answer) || hasText(pd?.assistant)
}

// ---------------------------------------------------------------------------
// create_data output classification
// ---------------------------------------------------------------------------

export type DataOutputKind = 'table' | 'chart' | 'number' | 'none'

export interface DataOutput {
  kind: DataOutputKind
  title: string
  /** Total rows of a table/chart result, when known. */
  rowCount: number | null
  /** The single observed value of a `number` result, ready to display. */
  value: string
}

const NUMBER_VIEW_TYPES = new Set(['count', 'metric_card'])

function viewTypeOf(te: any): string {
  const viz = Array.isArray(te?.created_visualizations) ? te.created_visualizations[0] : null
  const v = viz?.view
  const t = v?.view?.type || v?.type
    || te?.result_json?.data_model?.type
    || te?.created_step?.data_model?.type
    || ''
  return String(t).toLowerCase()
}

function dataOf(te: any): any {
  // The shared-link payload carries no stored step, only the result's preview.
  return te?.created_step?.data || te?.result_json?.data || te?.result_json?.widget_data
    || te?.result_json?.data_preview || null
}

function formatValue(v: any): string {
  if (v === null || v === undefined) return ''
  if (typeof v === 'number' && Number.isFinite(v)) {
    return v.toLocaleString(undefined, { maximumFractionDigits: 2 })
  }
  const s = String(v)
  return s.length > 60 ? `${s.slice(0, 57)}…` : s
}

/** The lone cell of a 1×1 result, or undefined when the result is not 1×1. */
function singleCell(data: any): any {
  const rows = data?.rows
  if (!Array.isArray(rows) || rows.length !== 1) return undefined
  const row = rows[0]
  if (Array.isArray(row)) return row.length === 1 ? row[0] : undefined
  if (row && typeof row === 'object') {
    const keys = Object.keys(row)
    return keys.length === 1 ? row[keys[0]] : undefined
  }
  return undefined
}

/** What a create_data call produced, from its own result — never guessed
    from the request. A 1×1 result is a number whatever view the model picked:
    a one-cell "table" has nothing a table preview could add. */
export function describeDataOutput(te: any): DataOutput {
  const title = String(
    te?.created_step?.title || te?.created_widget?.title || te?.arguments_json?.title || '',
  ).trim()
  const none: DataOutput = { kind: 'none', title, rowCount: null, value: '' }
  if (!te || te.status !== 'success') return none
  const data = dataOf(te)
  const rows = Array.isArray(data?.rows) ? data.rows : null
  const hasStep = !!(te.created_step || te.created_step_id || te.created_widget)
  if (!rows && !hasStep) return none

  let rowCount: number | null = rows ? rows.length : null
  // A preview holds the first rows only: report the server's total when it
  // says there are more.
  const totals = [data?.total_rows, data?.row_count, te?.result_json?.stats?.total_rows]
    .filter((n): n is number => typeof n === 'number')
  if (totals.length) rowCount = Math.max(rowCount || 0, ...totals)

  const type = viewTypeOf(te)
  const cell = singleCell(data)
  if (cell !== undefined && (typeof cell !== 'object' || cell === null)) {
    return { kind: 'number', title, rowCount, value: formatValue(cell) }
  }
  if (NUMBER_VIEW_TYPES.has(type)) return { kind: 'number', title, rowCount, value: '' }
  if (type && type !== 'table') return { kind: 'chart', title, rowCount, value: '' }
  return { kind: 'table', title, rowCount, value: '' }
}

// ---------------------------------------------------------------------------
// Deliverables
// ---------------------------------------------------------------------------

// Tools whose successful result is something the user opens or acts on. The
// list is explicit: an unknown tool is activity (one click away in the steps),
// never promoted to a card under the conclusion.
export const DELIVERABLE_TOOLS = new Set<string>([
  'create_data',
  'create_widget',
  'create_and_execute_code',
  'execute_code',
  'execute_sql',
  'create_dashboard',
  'create_artifact',
  'edit_artifact',
  'create_doc',
  'edit_doc',
  'generate_image',
  'write_csv',
  'write_to_excel',
  'attach_file',
  'submit_list',
  'create_list',
  'edit_list',
  'create_instruction',
  'edit_instruction',
  'suggest_instructions',
  'create_scheduled_task',
  'edit_scheduled_task',
  'create_prompt',
  'edit_prompt',
  'create_eval',
  'edit_eval',
  'run_eval',
  'create_agent',
  'create_demo_dataset',
  'send_email',
])

const CODE_TOOLS = new Set(['create_and_execute_code', 'execute_code', 'execute_sql'])

function isDeliverableTool(name: string): boolean {
  return DELIVERABLE_TOOLS.has(name) || (name.startsWith('submit_') && name !== 'submit_list')
}

/** Identity of the thing a block delivered. Two blocks with the same key are
    revisions of one deliverable — only the latest stays under the conclusion.
    Empty when the block delivered nothing. */
export function deliverableKey(block: any): string {
  const te = block?.tool_execution
  const name = String(te?.tool_name || '')
  if (!te || !isDeliverableTool(name) || isBlockFailed(block) || te.status !== 'success') return ''
  const rj = te.result_json || {}
  const aj = te.arguments_json || {}
  if (name === 'create_data') {
    const out = describeDataOutput(te)
    if (out.kind !== 'table' && out.kind !== 'chart') return ''
    return `data:${(out.title || String(te.created_step_id || block.id)).toLowerCase()}`
  }
  if (CODE_TOOLS.has(name)) {
    return te.created_widget || te.created_step ? `data:${String(te.created_step_id || block.id)}` : ''
  }
  if (name === 'create_artifact' || name === 'edit_artifact' || name === 'create_dashboard') {
    const id = rj.artifact_id || aj.artifact_id
    return id ? `artifact:${id}` : `block:${block.id}`
  }
  if (name === 'create_doc' || name === 'edit_doc') {
    const id = rj.doc_id || aj.doc_id
    return id ? `doc:${id}` : `block:${block.id}`
  }
  return `block:${block.id}`
}

// ---------------------------------------------------------------------------
// Steps
// ---------------------------------------------------------------------------

const INSPECT_TOOLS = new Set(['inspect_data', 'describe_tables', 'describe_entity'])

export type RunStepKind = 'inspect' | 'data' | 'action'

export interface RunStep {
  /** Id of the first member block — the step header renders in that block. */
  id: string
  kind: RunStepKind
  blockIds: string[]
  count: number
  /** Members that failed (the run carried on past them). */
  issueCount: number
  /** i18n key under `reportView.runFold.steps`, with its params. */
  labelKey: string
  labelParams: Record<string, any>
  /** Model-written title, already in the response language. Wins over labelKey. */
  labelText: string
  /** What was inspected (table/entity names), for `inspect` steps. */
  subjects: string[]
  /** What a `data` step produced. */
  output: DataOutput | null
}

export interface RunActivity {
  finalBlockId: string
  steps: RunStep[]
  /** blockId -> the step its tool call belongs to */
  stepOf: Record<string, RunStep>
  /** blockId of each step's first member -> step (header render anchor) */
  headerAt: Record<string, RunStep>
  /** Blocks whose output stays reachable below the final message, in order. */
  deliverableIds: string[]
  /** First activity start -> final message start, or null without timestamps. */
  spanMs: number | null
}

function names(v: any): string[] {
  const list = Array.isArray(v) ? v : (v === null || v === undefined || v === '' ? [] : [v])
  return list
    .map((x) => (typeof x === 'string' ? x : (x?.name ?? x?.table ?? '')))
    .map((x) => String(x || '').trim())
    .filter(Boolean)
}

/** Table names an inspection call looked at. */
export function inspectedSubjects(te: any): string[] {
  const aj = te?.arguments_json || {}
  const out: string[] = []
  const push = (n: string) => { if (!out.some((o) => o.toLowerCase() === n.toLowerCase())) out.push(n) }
  if (Array.isArray(aj.tables_by_source)) {
    for (const g of aj.tables_by_source) names(g?.tables).forEach(push)
  }
  if (te?.tool_name === 'describe_tables') {
    names(aj.query ?? te?.result_json?.search_query).forEach(push)
  }
  if (te?.tool_name === 'describe_entity') {
    names(te?.result_json?.title || aj.name_or_id).forEach(push)
  }
  return out
}

// A describe_tables query is a name OR a pattern; "Checked the cust.* table"
// would misname what happened.
const PATTERN_RE = /[*?^$|()[\]{}+\\]/

function purposeOf(block: any): { key: string; kind: RunStepKind; subjects: string[] } {
  const te = block.tool_execution
  const name = String(te?.tool_name || '')
  if (name === 'create_data') return { key: `data:${block.id}`, kind: 'data', subjects: [] }
  if (INSPECT_TOOLS.has(name)) {
    const subjects = inspectedSubjects(te)
    const sorted = subjects.map((s) => s.toLowerCase()).sort().join('|')
    const scope = name === 'describe_entity' ? 'entity' : 'table'
    return { key: `inspect:${scope}:${sorted}`, kind: 'inspect', subjects }
  }
  const family = name.startsWith('mcp__') ? 'mcp' : (name.startsWith('submit_') ? 'submit_list' : name)
  return { key: `tool:${family}`, kind: 'action', subjects: [] }
}

function llmTitle(block: any): string {
  const t = block?.tool_execution?.arguments_json?.title
  return typeof t === 'string' ? t.trim() : ''
}

/** Plain-language family for a tool with no model-written title. */
function actionLabelKey(toolName: string): string {
  if (toolName === 'execute_mcp' || toolName === 'call_tool' || toolName.startsWith('mcp__')) return 'usedTool'
  if (toolName.startsWith('search_') || toolName.startsWith('grep_') || toolName === 'web_search') return 'searched'
  if (toolName.startsWith('read_') || toolName === 'web_fetch') return 'read'
  if (toolName.startsWith('list_') || toolName.startsWith('get_')) return 'lookedUp'
  if (toolName.startsWith('browser_')) return 'browsed'
  if (toolName.startsWith('create_') || toolName.startsWith('generate_') || toolName.startsWith('write_') || toolName.startsWith('submit_')) return 'created'
  if (toolName.startsWith('edit_')) return 'updated'
  return 'ran'
}

function humanToolName(toolName: string): string {
  return toolName.replace(/^mcp__/, '').replace(/_+/g, ' ').trim()
}

function buildStep(run: any[], kind: RunStepKind, subjects: string[]): RunStep {
  const first = run[0]
  const te = first.tool_execution
  const name = String(te?.tool_name || '')
  const issueCount = run.filter((b) => isBlockFailed(b)).length
  let labelKey = 'ran'
  let labelParams: Record<string, any> = {}
  let labelText = ''
  let output: DataOutput | null = null

  if (kind === 'data') {
    output = describeDataOutput(te)
    const title = output.title
    if (output.kind === 'table') labelKey = title ? 'createdTable' : 'createdTableUntitled'
    else if (output.kind === 'chart') labelKey = title ? 'createdChart' : 'createdChartUntitled'
    else if (output.kind === 'number') labelKey = title ? 'calculated' : 'calculatedUntitled'
    else if (isBlockFailed(first)) labelKey = title ? 'dataFailed' : 'dataFailedUntitled'
    else labelKey = title ? 'preparedData' : 'preparedDataUntitled'
    labelParams = { title }
  } else if (kind === 'inspect') {
    if (!subjects.length) {
      labelKey = 'inspectedData'
    } else if (name === 'describe_entity') {
      labelKey = 'inspectedEntity'
      labelParams = { name: subjects[0] }
    } else if (subjects.length === 1) {
      labelKey = PATTERN_RE.test(subjects[0]) ? 'lookedForTables' : 'inspectedTable'
      labelParams = { name: subjects[0] }
    } else {
      labelKey = 'inspectedTables'
      labelParams = { count: subjects.length }
    }
  } else {
    // The last member's title describes where the step ended up; a lone
    // member's title is simply what it did.
    for (const b of run) { const t = llmTitle(b); if (t) labelText = t }
    labelKey = actionLabelKey(name)
    labelParams = { tool: humanToolName(name) }
  }

  return {
    id: String(first.id),
    kind,
    blockIds: run.map((b) => String(b.id)),
    count: run.length,
    issueCount,
    labelKey,
    labelParams,
    labelText,
    subjects,
    output,
  }
}

function hasReasoning(block: any): boolean {
  const hasText = (v: any) => typeof v === 'string' && v.trim() !== ''
  return hasText(block?.reasoning) || hasText(block?.plan_decision?.reasoning)
}

function toMs(v: any): number | null {
  if (v === null || v === undefined || v === '') return null
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  const s = String(v)
  // Server timestamps are naive UTC unless they carry a zone.
  const t = Date.parse(/Z|[+-]\d{2}:?\d{2}$/.test(s) ? s : `${s}Z`)
  return Number.isFinite(t) ? t : null
}

/** The block that carries the run's conclusion, or null while there is none. */
export function findFinalBlock(blocks: any[], runStatus?: string): any | null {
  const last = (blocks || [])[blocks.length - 1]
  if (!last || !blockHasMessage(last)) return null
  if (last.status === 'error' || last.tool_execution?.tool_name === 'clarify') return null
  const te = last.tool_execution
  if (te && !['success', 'error', 'failed'].includes(String(te.status || ''))) return null
  const over = runStatus === 'success' || (!!last.plan_decision?.analysis_complete && runStatus !== 'error' && runStatus !== 'stopped')
  return over ? last : null
}

/**
 * Fold a run around its final message.
 *
 * `blocks` is the ordered list the timeline renders. Returns null while the
 * run has no conclusion yet, or when nothing preceded it (a direct answer has
 * no steps to view).
 *
 * `breakBefore(block)` forces a step boundary for reasons outside the block
 * (a steering bubble interleaved before it). `isSuperseded(block)` marks
 * blocks the page already hides behind another card (edit runs) so they are
 * not promoted as deliverables twice.
 */
export function computeRunActivity(
  blocks: any[],
  opts?: {
    runStatus?: string
    breakBefore?: (block: any) => boolean
    isSuperseded?: (block: any) => boolean
  },
): RunActivity | null {
  const list = blocks || []
  const final = findFinalBlock(list, opts?.runStatus)
  if (!final) return null
  const before = list.slice(0, -1)
  if (!before.length && !hasReasoning(final) && !final.tool_execution) return null

  const steps: RunStep[] = []
  const stepOf: Record<string, RunStep> = {}
  const headerAt: Record<string, RunStep> = {}
  let run: any[] = []
  let runPurpose: ReturnType<typeof purposeOf> | null = null

  const flush = () => {
    if (run.length && runPurpose) {
      const step = buildStep(run, runPurpose.kind, runPurpose.subjects)
      steps.push(step)
      headerAt[step.id] = step
      for (const b of run) stepOf[String(b.id)] = step
    }
    run = []
    runPurpose = null
  }

  for (const b of list) {
    if (!b?.tool_execution) { flush(); continue }
    const purpose = purposeOf(b)
    const continues = !!runPurpose
      && runPurpose.key === purpose.key
      && purpose.kind !== 'data'
      && !blockHasMessage(b)
      && !opts?.breakBefore?.(b)
    if (!continues) flush()
    if (!runPurpose) runPurpose = purpose
    run.push(b)
  }
  flush()

  // Latest revision of each deliverable, in the order they were produced.
  const latest = new Map<string, string>()
  for (const b of list) {
    if (opts?.isSuperseded?.(b)) continue
    const key = deliverableKey(b)
    if (!key) continue
    latest.delete(key)
    latest.set(key, String(b.id))
  }

  const starts = list.map((b) => toMs(b?.started_at) ?? toMs(b?.tool_execution?.started_at)).filter((t): t is number => t !== null)
  const finalStart = toMs(final.started_at)
  const spanMs = starts.length && finalStart !== null && finalStart >= Math.min(...starts)
    ? finalStart - Math.min(...starts)
    : null

  return {
    finalBlockId: String(final.id),
    steps,
    stepOf,
    headerAt,
    deliverableIds: Array.from(latest.values()),
    spanMs,
  }
}
