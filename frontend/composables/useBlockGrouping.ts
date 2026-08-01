/**
 * Grouping of consecutive low-signal tool blocks in the completion stream
 * (Codex-style "worked through N steps" clusters).
 *
 * Pure functions — no Vue reactivity — so the grouping policy is unit-testable
 * and shared verbatim by the report view and the public share view.
 *
 * Policy (see PR #837 discussion):
 * - Only "chip-class" blocks group: read-only/research tools that completed
 *   successfully and carry no user-facing answer text. The allowlist is
 *   explicit — unknown tools stay visible.
 * - Deliverables (create_data, artifacts, docs), clarify, approvals, the plan
 *   note, errors, and in-flight blocks NEVER fold: content and failures must
 *   stay visible, and the live block is the user's status line.
 * - ANY run of two or more consecutive chip-class blocks folds into one
 *   "live ticker" line (single morphing label while running, compact summary
 *   when settled) — one visual grammar for every run, no threshold split.
 * - Group headers are synthesized deterministically from what the run did
 *   (verb families + duration) plus the last block's LLM-generated `title`
 *   arg — no extra LLM call.
 */

// Read-only / research / bookkeeping tools that may fold into a group.
export const GROUPABLE_TOOLS = new Set<string>([
  'describe_tables',
  'describe_entity',
  'read_resources',
  'read_instruction',
  'search_instructions',
  'inspect_data',
  'list_files',
  'search_files',
  'grep_files',
  'read_file',
  'list_mcp_resources',
  'read_mcp_resource',
  'search_mcps',
  'search_agents',
  'read_report',
  'search_reports',
  'read_query',
  'read_artifact',
  'list_connections',
  'get_connection',
  'search_prompts',
  'search_evals',
  'get_eval_run',
  'get_eval_runs',
  'list_agent_executions',
  'list_emails',
  'read_email',
  'search_email',
  'web_fetch',
  'execute_mcp',
  'edit_note',
  // Browser navigation/reading/interaction are low-signal; a browsing run
  // collapses to one ticker line. browser_vision stays OUT so a screenshot
  // renders as its own card (mirrors web_fetch in / generate_image out).
  'browser_navigate',
  'browser_snapshot',
  'browser_extract',
  'browser_act',
])

// Minimum consecutive chip-class blocks before a group forms. Two or more —
// a lone call renders as a normal row, everything longer becomes one ticker
// line so the page never mixes grouped and stacked grammars for runs.
export const MIN_GROUP_RUN = 2

const FAILED_STATUSES = new Set(['error', 'failed', 'stopped'])

/** Whether a block failed (tool error or block error). */
export function isBlockFailed(block: any): boolean {
  const te = block?.tool_execution
  return (
    FAILED_STATUSES.has(String(te?.status || '')) ||
    FAILED_STATUSES.has(String(block?.status || ''))
  )
}

/** Whether a block has finished (tool done, block closed). Failed blocks
    count as settled — they're over, not running. */
export function isBlockSettled(block: any): boolean {
  if (isBlockFailed(block)) return true
  const te = block?.tool_execution
  const teDone = !te || (te.status || '') === 'success'
  const bs = String(block?.status || '')
  return teDone && (bs === 'completed' || bs === 'success' || !!block?.completed_at)
}

/** Error taxonomy (see PR #837 review):
    - "handled":     a probe that failed but the run continued past it — amber,
                     absorbed into the group, counted on the header.
    - "actionable":  the USER must do something (sign in / connect) — never
                     folded; rendered as a call-to-action row.
    - "fatal":       nothing recovered after it (it is the last block) — red,
                     never folded.
    Returns null for non-failed blocks. */
export function classifyBlockError(block: any, isLast: boolean): 'handled' | 'actionable' | 'fatal' | null {
  if (!isBlockFailed(block)) return null
  const te = block?.tool_execution
  const text = `${te?.result_summary || ''} ${block?.content || ''}`.toLowerCase()
  if (/sign.?in|connect this|needs the user|oauth|credential|access token|401|unauthorized/.test(text)) {
    return 'actionable'
  }
  return isLast ? 'fatal' : 'handled'
}

/** Whether a block may fold into a group.

    In-flight blocks of groupable tools fold too — the group header is the
    live status line while the run works through a research phase (the
    running step's label renders in the header). Failures never fold, and
    grouping recomputes reactively, so a member that later errors pops back
    out on its own. */
export function isGroupableBlock(block: any): boolean {
  const te = block?.tool_execution
  if (!te || !GROUPABLE_TOOLS.has(te.tool_name)) return false
  if (FAILED_STATUSES.has(String(te.status || ''))) return false
  if (FAILED_STATUSES.has(String(block?.status || ''))) return false
  // A block that carries the final answer (or any user-directed prose beyond
  // the pre-tool sentence rendered inside the thinking box) is content.
  if (block?.plan_decision?.final_answer) return false
  return true
}

/** Human label for a running tool, used when the block carries no LLM
    `title` arg (e.g. inspect_data/describe_tables don't take one). */
export function humanToolLabel(toolName: string): string {
  const map: Record<string, string> = {
    describe_tables: 'Describing tables',
    describe_entity: 'Describing entity',
    inspect_data: 'Inspecting data',
    read_resources: 'Reading resources',
    read_instruction: 'Reading instructions',
    search_instructions: 'Searching instructions',
    list_files: 'Listing files',
    search_files: 'Searching files',
    grep_files: 'Searching files',
    read_file: 'Reading a file',
    list_mcp_resources: 'Listing resources',
    read_mcp_resource: 'Reading a resource',
    search_mcps: 'Searching tools',
    search_agents: 'Searching agents',
    read_report: 'Reading a report',
    search_reports: 'Searching reports',
    read_query: 'Reading a query',
    read_artifact: 'Reading the artifact',
    execute_mcp: 'Calling a tool',
    edit_note: 'Updating notes',
    web_fetch: 'Fetching a page',
  }
  return map[toolName] || 'Working'
}

/** Verb family for the header summary ("4 reads · 2 searches"). */
function verbFamily(toolName: string): string {
  if (!toolName) return 'steps'
  if (toolName === 'execute_mcp') return 'tool calls'
  if (toolName === 'edit_note') return 'note updates'
  if (toolName === 'inspect_data') return 'inspections'
  if (toolName.startsWith('search_') || toolName === 'grep_files') return 'searches'
  if (toolName.startsWith('read_') || toolName === 'web_fetch') return 'reads'
  if (toolName.startsWith('list_')) return 'lists'
  if (toolName.startsWith('describe_')) return 'lookups'
  if (toolName.startsWith('get_')) return 'lookups'
  return 'steps'
}

export interface BlockGroup {
  id: string
  blockIds: string[]
  count: number
  /** Elapsed wall-clock across the run (earliest member start -> latest member
      end), so inter-step LLM planning time is included. Falls back to the sum
      of per-tool self-times only when members carry no timestamps. */
  durationMs: number
  /** True when the figure covers the whole run (span with every member ended,
      or — in the fallback — every non-failed member reported a duration).
      Otherwise the header omits the misleading partial figure. */
  durationComplete: boolean
  /** Handled-error members absorbed into this group (amber count on header). */
  issueCount: number
  /** "4 reads · 2 searches" */
  verbSummary: string
  /** LLM-generated `title` of the LAST block in the group, if any. */
  lastTitle: string
  /** Tool names in first-appearance order (for icon strips). */
  toolNames: string[]
  /** True while a member is still running — the header is then the live
      status line (spinner + shimmer + runningLabel). */
  active: boolean
  /** What the running member is doing: its LLM `title` arg, else a
      humanized tool label. Empty when the group is settled. */
  runningLabel: string
}

export interface BlockGrouping {
  /** blockId -> group it belongs to */
  groupOf: Record<string, BlockGroup>
  /** blockId of each group's FIRST block -> group (header render anchor) */
  headerAt: Record<string, BlockGroup>
}

function llmTitle(block: any): string {
  const te = block?.tool_execution
  const t = te?.arguments_json?.title
  return typeof t === 'string' ? t.trim() : ''
}

/** Parse an ISO/epoch timestamp to epoch ms, or null if absent/unparseable.
    Backend serializes timestamps as UTC ISO strings with a `Z` suffix, so
    Date.parse resolves them unambiguously. */
function toEpochMs(v: any): number | null {
  if (v === null || v === undefined || v === '') return null
  const t = typeof v === 'number' ? v : Date.parse(v)
  return Number.isFinite(t) ? t : null
}

/** Earliest start of a member: prefer the block span (which brackets this
    step's LLM planning + tool run — block.started_at is the plan decision's
    creation), falling back to the tool execution's own start. */
function memberStartMs(b: any): number | null {
  return toEpochMs(b?.started_at) ?? toEpochMs(b?.tool_execution?.started_at)
}

/** Latest end of a member: block completion mirrors the tool's completion;
    fall back to the tool execution's own end. */
function memberEndMs(b: any): number | null {
  return toEpochMs(b?.completed_at) ?? toEpochMs(b?.tool_execution?.completed_at)
}

// Family labels are stored as plurals; explicit singulars — a regex
// singularizer produced "1 note updat".
const SINGULAR: Record<string, string> = {
  'reads': 'read',
  'searches': 'search',
  'lists': 'list',
  'lookups': 'lookup',
  'inspections': 'inspection',
  'tool calls': 'tool call',
  'note updates': 'note update',
  'steps': 'step',
}

function buildGroup(run: any[]): BlockGroup {
  const famCounts = new Map<string, number>()
  const toolNames: string[] = []
  // Sum of per-tool self-times — the fallback when members carry no
  // timestamps (older reports / unit fixtures).
  let sumMs = 0
  let sumKnown = 0
  let okMembers = 0
  let lastTitle = ''
  let active = false
  let runningLabel = ''
  let issueCount = 0
  // Wall-clock span material: earliest member start -> latest member end.
  let minStart = Infinity
  let maxEnd = -Infinity
  let startsKnown = 0
  let endsKnown = 0
  for (const b of run) {
    const name = b?.tool_execution?.tool_name || ''
    if (name && !toolNames.includes(name)) toolNames.push(name)
    const fam = verbFamily(name)
    famCounts.set(fam, (famCounts.get(fam) || 0) + 1)
    // Every member (including a handled-error probe) counts toward the span —
    // it consumed elapsed time before the run moved on.
    const s = memberStartMs(b)
    const e = memberEndMs(b)
    if (s !== null) { startsKnown += 1; if (s < minStart) minStart = s }
    if (e !== null) { endsKnown += 1; if (e > maxEnd) maxEnd = e }
    if (isBlockFailed(b)) {
      issueCount += 1
    } else {
      okMembers += 1
      const d = Number(b?.tool_execution?.duration_ms || 0)
      if (d > 0) { sumMs += d; sumKnown += 1 }
      const t = llmTitle(b)
      if (t) lastTitle = t
      if (!isBlockSettled(b)) {
        active = true
        runningLabel = t || humanToolLabel(name)
      }
    }
  }
  // Prefer elapsed wall-clock over the sum of per-tool self-times. duration_ms
  // measures only each tool's own execution and excludes the LLM planning
  // between steps, so summing it makes a several-second group read as "1s".
  // The span from the first member's start to the last member's end includes
  // those inter-step gaps — the time the user actually waited. Fall back to the
  // sum only when members carry no usable timestamps.
  const spanValid = startsKnown === run.length && endsKnown > 0 && maxEnd >= minStart
  let durationMs: number
  let durationComplete: boolean
  if (spanValid) {
    durationMs = maxEnd - minStart
    // Trust the span's end only when every member reported one. A missing end
    // means a member is still in flight — but then `active` is true and the
    // duration isn't rendered anyway.
    durationComplete = endsKnown === run.length
  } else {
    durationMs = sumMs
    durationComplete = okMembers > 0 && sumKnown === okMembers
  }
  const verbSummary = Array.from(famCounts.entries())
    .sort((a, b) => b[1] - a[1])
    .map(([fam, n]) => `${n} ${n === 1 ? (SINGULAR[fam] || fam) : fam}`)
    .join(' · ')
  return {
    id: String(run[0]?.id ?? ''),
    blockIds: run.map((b) => String(b?.id ?? '')),
    count: run.length,
    durationMs,
    durationComplete,
    issueCount,
    verbSummary,
    lastTitle,
    toolNames,
    active,
    runningLabel,
  }
}

/**
 * Compute groups over an ordered, already-filtered block list.
 *
 * `breakBefore(block)` lets the caller force a group boundary in front of a
 * block for reasons outside the block itself (e.g. a steering bubble is
 * interleaved before it in the report view).
 */
export function computeBlockGroups(
  blocks: any[],
  opts?: { minRun?: number; breakBefore?: (block: any) => boolean },
): BlockGrouping {
  const minRun = opts?.minRun ?? MIN_GROUP_RUN
  const groupOf: Record<string, BlockGroup> = {}
  const headerAt: Record<string, BlockGroup> = {}
  let run: any[] = []

  const flush = () => {
    if (run.length >= minRun) {
      const g = buildGroup(run)
      headerAt[String(run[0]?.id ?? '')] = g
      for (const b of run) groupOf[String(b?.id ?? '')] = g
    }
    run = []
  }

  const list = blocks || []
  for (let i = 0; i < list.length; i++) {
    const b = list[i]
    if (opts?.breakBefore?.(b)) flush()
    const te = b?.tool_execution
    const groupableTool = !!te && GROUPABLE_TOOLS.has(te.tool_name) && !b?.plan_decision?.final_answer
    const errCls = classifyBlockError(b, i === list.length - 1)
    if (errCls === 'handled' && groupableTool) {
      // Absorbed: the run continues around a handled probe failure — it is
      // counted on the header (amber) instead of fragmenting the group.
      run.push(b)
    } else if (!errCls && isGroupableBlock(b)) {
      run.push(b)
    } else {
      // Content, actionable/fatal errors, and non-groupable tools break runs.
      flush()
    }
  }
  flush()
  return { groupOf, headerAt }
}
