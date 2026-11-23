/**
 * Run-level activity fold for the report conversation.
 *
 * A run's activity sits behind one row. While the run works the row reads
 * "Working for 12s · <what it is doing> · View steps" and only the text being
 * streamed (and any card waiting on the user) stays open below it — tool
 * cards, create_data results and reasoning included stay folded. Once the
 * FINAL MESSAGE has arrived the row settles to "Worked for 10s · View steps"
 * above the conclusion. Expanding the row shows the chain, with consecutive
 * same-purpose tool calls merged under one plain-language step ("Checked the
 * customers table").
 *
 * Pure functions — no Vue reactivity — so the policy is unit-testable
 * (tests/unit/useRunActivity.mjs). Labels are returned as i18n keys + params;
 * the page translates them in the response's language.
 *
 * Policy:
 * - Live (run still in progress, no final message yet): the last block's
 *   prose is shown while that block has not started a tool call — it may be
 *   the answer being written, or a progress note that folds into the row as
 *   soon as its tool call starts. `keepOpen` blocks (an approval card) stay
 *   open too. The row's status is the latest progress note or step title.
 * - The final message is the LAST block, when it carries user-facing prose and
 *   the run is known to be over (the planner marked it `analysis_complete`, or
 *   the run's status is `success`). A run that stopped, errored, or ended on a
 *   question (clarify) or a sign-in request (request_agent_signin) has no
 *   conclusion and never folds: its failure, question or Connect card must
 *   stay in view.
 * - Prose always breaks a step: a progress message is part of the chain and
 *   keeps its chronological place. Reasoning does not — it rides inside the
 *   step it preceded.
 * - A data result (`create_data`, or `describe_entity` materializing a saved
 *   query — isDataResultCall) is always its own step, described by what it
 *   produced.
 * - Every data result — chart, table or single value — folds into its
 *   step. Results stay reachable from the answer itself through its citations
 *   and sources line (composables/useCitations.ts): hover previews them, click
 *   opens them. Keeping charts open above the answer was tried and dropped: a
 *   run that charts six results buried its conclusion under six cards.
 * - Other outputs the user would open or act on (dashboards, docs, review
 *   cards…) are returned as `deliverableIds` so the page can keep them
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

export function blockHasMessage(block: any): boolean {
  const pd = block?.plan_decision
  const hasText = (v: any) => typeof v === 'string' && v.trim() !== ''
  return hasText(block?.content) || hasText(pd?.final_answer) || hasText(pd?.assistant)
}

// ---------------------------------------------------------------------------
// Data results
// ---------------------------------------------------------------------------

/** A tool call that produced a tracked data result: a `create_data` call, or a
    `describe_entity` call in create mode (`should_create`), which materializes
    a saved query as its own step and visualization. Both fold into a `data`
    step and both are sources the answer can cite. A plain `describe_entity`
    lookup is inspection. Mirrored in useCitations (isDataResultCall), which
    stays dependency-free for the bundler-less unit tests. */
export function isDataResultCall(te: any): boolean {
  const name = String(te?.tool_name || '')
  if (name === 'create_data') return true
  if (name !== 'describe_entity') return false
  return te?.arguments_json?.should_create === true
    || !!(te?.created_step_id || te?.created_step || te?.created_widget)
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
    te?.created_step?.title || te?.created_widget?.title || te?.arguments_json?.title
      // describe_entity names its result after the saved query it materialized.
      || te?.result_json?.title || '',
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
// never promoted to a card under the conclusion. create_data is not here: its
// charts stay inline and its tables are sources of the answer (see header).
export const DELIVERABLE_TOOLS = new Set<string>([
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
  /** A member's tool call is still running (live runs only). */
  running: boolean
}

export interface RunActivity {
  /** True while the run is still working (no final message yet). */
  live: boolean
  /** Empty while live. */
  finalBlockId: string
  /**
   * Blocks shown as the answer while folded: the final block, preceded by the
   * prose-only block right before it when the final block also carries a tool
   * call. A run that writes its analysis and then ends on a short "Building
   * the dashboard now…" deliverable call keeps the analysis on screen.
   */
  answerIds: string[]
  steps: RunStep[]
  toolGroups: RunToolGroup[]
  /** blockId -> the step its tool call belongs to */
  stepOf: Record<string, RunStep>
  /** blockId of each step's first member -> step (header render anchor) */
  headerAt: Record<string, RunStep>
  /** Blocks whose output stays reachable below the final message, in order,
      followed by still-running work (`keepVisible`). */
  deliverableIds: string[]
  /** Blocks shown in full — prose and tool card — while folded (approval
      cards), and, while live, running work with a Stop control. */
  openIds: string[]
  /** What the run is doing now, for the live row: the latest progress note or
      step title, one line. Empty when finished or unknown. */
  status: string
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
  if (isDataResultCall(te)) return { key: `data:${block.id}`, kind: 'data', subjects: [] }
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

  const running = run.some((b) => RUNNING_STATUSES.has(String(b?.tool_execution?.status || '')))
  if (kind === 'data' && running) {
    labelKey = (llmTitle(first) ? 'creatingData' : 'creatingDataUntitled')
    labelParams = { title: llmTitle(first) }
  } else if (kind === 'data') {
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
    running,
  }
}

const RUNNING_STATUSES = new Set(['running', 'pending', 'in_progress', 'queued'])

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

// A run that ends on one of these is waiting for the user (an answer to a
// question, a sign-in to an agent): it has no conclusion, and its card must
// stay in view instead of folding into the steps.
const WAITS_ON_USER = new Set<string>(['clarify', 'request_agent_signin'])

/**
 * The block that ends the run's conclusion, or null while there is none. When
 * it also carries a tool call, the prose block right before it is part of the
 * answer too (`RunActivity.answerIds`).
 */
export function findFinalBlock(blocks: any[], runStatus?: string): any | null {
  const last = (blocks || [])[blocks.length - 1]
  if (!last || !blockHasMessage(last)) return null
  if (last.status === 'error' || WAITS_ON_USER.has(last.tool_execution?.tool_name)) return null
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
    /** Blocks that must stay open while folded (a card waiting on the user). */
    keepOpen?: (block: any) => boolean
    /** Blocks whose work outlives their tool call and that carry its Stop
        control (a running eval, a sub-agent still investigating). They are
        never hidden: open while live, kept under the answer once folded. */
    keepVisible?: (block: any) => boolean
  },
): RunActivity | null {
  const list = blocks || []
  const final = findFinalBlock(list, opts?.runStatus)
  const live = !final && opts?.runStatus === 'in_progress'
  if (!final && !live) return null
  const last = list[list.length - 1]
  // While live, the trailing block's prose is the text being streamed — until
  // that block starts a tool call.
  const streaming = live && last && !last.tool_execution && blockHasMessage(last) ? last : null
  const anchor = final || streaming
  const before = anchor ? list.slice(0, -1) : list
  // A finished direct answer has no steps to view. A live run keeps its row
  // from the first block on: dropping it while a first progress note streams
  // and bringing it back when that note's tool call starts would flicker.
  if (!live && !before.length && !hasReasoning(final) && !final.tool_execution) return null
  if (live && !list.length) return null

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
  // Still-running work keeps its card (and Stop) in view: open in place while
  // the run works, under the answer once it folds.
  const deliverableIds = Array.from(latest.values())
  const openIds = live && opts?.keepOpen ? list.filter((b) => opts.keepOpen!(b)).map((b) => String(b.id)) : []
  for (const b of list) {
    if (!opts?.keepVisible?.(b)) continue
    const target = live ? openIds : deliverableIds
    if (!target.includes(String(b.id))) target.push(String(b.id))
  }

  const starts = list.map((b) => toMs(b?.started_at) ?? toMs(b?.tool_execution?.started_at)).filter((t): t is number => t !== null)
  const finalStart = final ? toMs(final.started_at) : null
  const spanMs = starts.length && finalStart !== null && finalStart >= Math.min(...starts)
    ? finalStart - Math.min(...starts)
    : null

  const answerIds = anchor ? [String(anchor.id)] : []
  const lead = before[before.length - 1]
  if (final && final.tool_execution && lead && !lead.tool_execution && blockHasMessage(lead) && !opts?.breakBefore?.(final)) {
    answerIds.unshift(String(lead.id))
  }

  return {
    live,
    finalBlockId: final ? String(final.id) : '',
    answerIds,
    steps,
    // Progress notes and final prose stream through the same field. Only
    // settle the summary when completion is confirmed, never between calls.
    toolGroups: computeRunToolGroups(list, opts?.breakBefore, !live),
    stepOf,
    headerAt,
    deliverableIds,
    openIds,
    status: live ? liveStatus(before) : '',
    spanMs,
  }
}

/** One line for the live row: the newest progress note, else the newest tool
    title, among the folded blocks. */
function liveStatus(folded: any[]): string {
  for (let i = folded.length - 1; i >= 0; i--) {
    const b = folded[i]
    const pd = b?.plan_decision
    const prose = [b?.content, pd?.assistant].find((v) => typeof v === 'string' && v.trim() !== '')
    const text = prose || llmTitle(b)
    if (text) {
      const line = String(text).replace(/[*_`#>\[\]]/g, '').replace(/\s+/g, ' ').trim()
      if (line) return line.length > 120 ? `${line.slice(0, 117)}…` : line
    }
  }
  return ''
}

export interface RunToolGroup {
  id: string
  blockIds: string[]
  categories: string[]
  counts: Record<string, number>
  settled: boolean
  failed?: boolean
  /** Members whose call failed, including ones the run recovered from; shown
      as a count on the row so a collapsed group never looks clean. */
  failedCount: number
  current: { name: string; title: string; status: string }
  icon: string
  running: boolean
}

function toolCategory(name: string): string {
  if (/browser|web_fetch/.test(name)) return 'browser'
  if (/search|grep/.test(name)) return 'search'
  if (/read_file|list_files/.test(name)) return 'files'
  if (/inspect|describe|list_tables/.test(name)) return 'inspect'
  if (name === 'create_data') return 'data'
  if (/execute_sql|execute_code|create_and_execute_code/.test(name)) return 'data'
  if (/create_|edit_|write_|generate_/.test(name)) return 'create'
  return 'tools'
}
const GROUP_ICONS: Record<string, string> = {
  browser: 'heroicons-globe-alt', search: 'heroicons-magnifying-glass',
  files: 'heroicons-book-open', inspect: 'heroicons-table-cells',
  results: 'heroicons-chart-bar', data: 'heroicons-chart-bar', create: 'heroicons-pencil-square', tools: 'heroicons-wrench-screwdriver',
}

/** Consecutive calls share a stable row; commentary and steering start a new
    group. A deliverable call (a dashboard, an instruction, a doc… — see
    DELIVERABLE_TOOLS) is always its own one-call group, so its card renders as
    a plain row instead of hiding behind a generic "Created and updated
    results" header. */
export function computeRunToolGroups(blocks: any[], breakBefore?: (block: any) => boolean, settled = true): RunToolGroup[] {
  const groups: RunToolGroup[] = []
  let group: RunToolGroup | undefined
  for (const block of blocks) {
    if (blockHasMessage(block) || breakBefore?.(block)) group = undefined
    const te = block.tool_execution
    if (!te) { group = undefined; continue }
    const name = String(te.tool_name || '')
    const standalone = isDeliverableTool(name)
    if (standalone) group = undefined
    const category = toolCategory(name)
    if (!group) {
      group = { id: String(block.id), blockIds: [], categories: [], counts: {}, settled, failedCount: 0, current: { name: '', title: '', status: '' }, icon: GROUP_ICONS[category], running: false }
      groups.push(group)
    }
    group.blockIds.push(String(block.id))
    group.counts[category] = (group.counts[category] || 0) + 1
    if (!group.categories.includes(category)) group.categories.push(category)
    const running = RUNNING_STATUSES.has(String(te.status || ''))
    const callFailed = !running && (['error', 'failed'].includes(String(te.status || '')) || !!te.error_message || !!te.result_json?.error || te.result_json?.success === false)
    if (callFailed) group.failedCount += 1
    // Prefer the latest running call; otherwise follow the latest completed call.
    if (running || !group.running) {
      group.icon = GROUP_ICONS[category]
      group.current = {
        name: String(te.tool_name || ''),
        title: String(te.created_step?.title || te.arguments_json?.title || '').trim(),
        status: String(te.status || ''),
      }
      // Match the warning to the displayed call; historical failures stay on their own rows.
      group.failed = callFailed
    }
    group.running ||= running
    if (standalone) group = undefined
  }
  return groups
}
