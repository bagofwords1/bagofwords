/**
 * What the agent-sandbox tool blocks (components/tools/RunInSandboxTool.vue,
 * SandboxTool.vue) read out of a tool call. Pure functions, no Vue: the report
 * view and the shared link use the same rules.
 */

export const SANDBOX_LANGUAGES: string[] = ['python', 'node', 'go', 'bash']
export const SANDBOX_NETWORK_POLICIES: string[] = ['none', 'registries', 'allowlist', 'full']
/** Stages `run_in_sandbox` reports while it works (tool.progress). */
export const SANDBOX_RUN_STAGES: string[] = ['starting', 'created', 'resumed', 'installing_packages', 'running', 'collecting_artifacts']

export type SandboxRunOutcome =
  | 'awaiting' | 'running' | 'succeeded' | 'failed' | 'timeout' | 'cancelled'
  | 'declined' | 'notConfirmed' | 'error'

const RUNNING = new Set(['running', 'in_progress', 'pending', 'queued'])
const AWAITING_STAGES = new Set(['awaiting_confirmation', 'awaiting_approval'])

/** The call is paused on an Allow / Deny card nobody has answered yet. */
export function sandboxAwaitsApproval(te: any, answered = false): boolean {
  return !!te?.confirmation?.confirmation_id && !answered
    && RUNNING.has(String(te?.status || '')) && AWAITING_STAGES.has(String(te?.progress_stage || ''))
}

/** A refusal reads "not confirmed in time" when nobody answered the card. */
function refusal(result: any): 'notConfirmed' | 'declined' {
  return /not confirmed in time/i.test(String(result?.error_message || '')) ? 'notConfirmed' : 'declined'
}

/** How a `run_in_sandbox` call stands, from the live row or the stored one. */
export function sandboxRunOutcome(te: any, runStatus = '', answered = false): SandboxRunOutcome {
  const status = String(te?.status || '')
  const result = te?.result_json || {}
  if (RUNNING.has(status)) {
    // The turn was stopped under a call that never reported back.
    if (runStatus === 'stopped' || runStatus === 'error') return 'cancelled'
    return sandboxAwaitsApproval(te, answered) ? 'awaiting' : 'running'
  }
  if (result.error_code === 'sandbox.declined') return refusal(result)
  if (['succeeded', 'failed', 'timeout', 'cancelled'].includes(result.status)) return result.status
  if (status === 'stopped' || status === 'cancelled') return 'cancelled'
  if (result.success === false || status === 'error' || status === 'failed') return 'error'
  return 'succeeded'
}

/** The state of a create / status / delete call. */
export type SandboxActionOutcome = 'awaiting' | 'running' | 'done' | 'declined' | 'notConfirmed' | 'cancelled' | 'error'

export function sandboxActionOutcome(te: any, runStatus = '', answered = false): SandboxActionOutcome {
  const status = String(te?.status || '')
  const result = te?.result_json || {}
  if (RUNNING.has(status)) {
    if (runStatus === 'stopped' || runStatus === 'error') return 'cancelled'
    return sandboxAwaitsApproval(te, answered) ? 'awaiting' : 'running'
  }
  if (result.error_code === 'sandbox.declined') return refusal(result)
  if (status === 'stopped' || status === 'cancelled') return 'cancelled'
  if (result.success === false || status === 'error' || status === 'failed') return 'error'
  return 'done'
}

/** One line for the collapsed row: the command, else the first line of the
    code that says something. Empty when the viewer may not see code. */
export function sandboxRunSummary(args: any): string {
  const command = typeof args?.command === 'string' ? args.command.trim() : ''
  const source = command || (typeof args?.code === 'string' ? args.code : '')
  const lines = source.split('\n').map((l: string) => l.trim()).filter(Boolean)
  // A leading shebang or comment says less than the first statement.
  const line = lines.find((l: string) => !/^(#|\/\/)/.test(l)) || lines[0] || ''
  return line.length > 96 ? `${line.slice(0, 95)}…` : line
}

/** The server blanks `code`, `command` and `files` for a viewer without
    `view_code` (backend/app/core/code_visibility.py): the keys are still there,
    with no value. */
export function sandboxCodeRedacted(args: any): boolean {
  if (!args || typeof args !== 'object') return false
  return ['code', 'command'].some((k) => k in args) && args.code == null && args.command == null
}

export type SandboxNoteKind =
  | 'fresh' | 'restored' | 'filesRestored' | 'created' | 'resumed' | 'deletion' | 'background'
  | 'timeout' | 'cancelled' | 'skipped' | 'other'

/** The codes a run's result carries in `note_codes`, as the kinds this client shows. */
export const SANDBOX_NOTE_CODES: Record<string, SandboxNoteKind> = {
  created: 'created',
  resumed: 'resumed',
  recreated: 'fresh',
  restored_snapshot: 'restored',
  files_restored: 'filesRestored',
  timeout: 'timeout',
  cancelled: 'cancelled',
  skipped_artifacts: 'skipped',
  background_processes: 'background',
  deletion_scheduled: 'deletion',
}

/**
 * What a note of a run is about, from its English sentence. Only for results
 * stored before the tool returned `note_codes`, and to tell which sentences in
 * the notes list repeat what the block already says. An unrecognized sentence
 * is still shown, as itself.
 */
export function sandboxNoteKind(note: string): SandboxNoteKind {
  const s = String(note || '')
  if (/had to be recreated/i.test(s)) return /restored from a snapshot/i.test(s) ? 'restored' : 'fresh'
  if (/new sandbox was created/i.test(s)) return 'created'
  if (/has been resumed/i.test(s)) return 'resumed'
  if (/will be deleted on/i.test(s)) return 'deletion'
  if (/still running in the sandbox/i.test(s)) return 'background'
  if (/stopped at its time limit/i.test(s)) return 'timeout'
  if (/run was cancelled/i.test(s)) return 'cancelled'
  if (/were not saved; see skipped_artifacts/i.test(s)) return 'skipped'
  return 'other'
}

/**
 * What a run's result says about its sandbox, in the order given. From
 * `note_codes` when the result has them (a code this client does not know is
 * left out); otherwise from the wording of `notes`, as older results need.
 */
export function sandboxResultNoteKinds(result: any): SandboxNoteKind[] {
  const kinds: SandboxNoteKind[] = Array.isArray(result?.note_codes)
    ? result.note_codes.map((code: unknown) => SANDBOX_NOTE_CODES[String(code)]).filter(Boolean)
    : (Array.isArray(result?.notes) ? result.notes : []).map(sandboxNoteKind).filter((kind: SandboxNoteKind) => kind !== 'other')
  return [...new Set(kinds)]
}

/** Notes that change what the reader should expect: shown on the collapsed row. */
export const SANDBOX_WARNING_NOTES: SandboxNoteKind[] = ['fresh', 'restored', 'deletion', 'background']
/** The kinds that get a chip under the row; the status line and the skipped list say the rest. */
export const SANDBOX_CHIP_NOTES: SandboxNoteKind[] = ['fresh', 'restored', 'filesRestored', 'deletion', 'background', 'created', 'resumed']

/** Why the card asks, as a locale key under tools.sandbox.confirm.reason, or
    '' for a sentence this client does not know (shown as sent). */
export function sandboxConfirmReasonKey(reason: string): string {
  const s = String(reason || '')
  if (/full internet access/i.test(s)) return 'fullNetwork'
  if (/installs packages/i.test(s)) return 'packages'
  if (/asks before every run/i.test(s)) return 'always'
  return ''
}

/** 'image' and 'pdf' open in the report's file panel; the rest download. */
export function sandboxFileKind(file: { name?: string; type?: string | null }): 'image' | 'pdf' | 'file' {
  const type = String(file?.type || '').toLowerCase()
  const name = String(file?.name || '').toLowerCase()
  if (type === 'application/pdf' || name.endsWith('.pdf')) return 'pdf'
  // SVG is markup, not a picture the image viewer should run.
  if ((type.startsWith('image/') && type !== 'image/svg+xml') || /\.(png|jpe?g|gif|webp)$/.test(name)) return 'image'
  return 'file'
}
