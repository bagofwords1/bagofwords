// The admin API for agent sandboxes (/api/sandbox/*), as calls that throw.
//
// `useMyFetch` answers `{ data, error }`; every caller in the Sandbox section
// wants the body or a thrown error it can hand to `useErrorMessage`, so the
// unwrapping lives here once.

export interface SandboxBootstrapStep {
  key: string
  title: string
  status: 'pending' | 'running' | 'ok' | 'warning' | 'failed' | 'skipped'
  detail?: string | null
  code?: string | null
}

export interface SandboxCompute {
  id: string
  name: string
  type: string
  config: Record<string, any>
  credentials_set: string[]
  capabilities: string[]
  labels: string[]
  capacity: number
  enabled: boolean
  bootstrap_status: string
  bootstrap_steps: SandboxBootstrapStep[]
  bootstrapped_at?: string | null
  status: string
  last_checked_at?: string | null
  last_error?: string | null
  info: Record<string, any>
  running_sandboxes: number
  // Storage the operator made available on it, by name.
  volumes?: SandboxComputeVolume[]
  // What the compute has; a missing key means it did not report that resource.
  resources?: SandboxResources
  // The allocation policy, with its defaults filled in.
  allocation?: Partial<SandboxAllocation>
  // What sandboxes may be given in total, and what they hold now by their limits.
  allocatable?: SandboxResources
  allocated?: SandboxResources & { running?: number; existing?: number }
}

export interface SandboxComputeVolume { name: string; source: string; description?: string | null }
export interface SandboxResources { cpus?: number; memory_mb?: number; disk_mb?: number }
export interface SandboxAllocation {
  cpu_overcommit: number
  memory_overcommit: number
  reserve_memory_mb: number
  reserve_disk_mb: number
}
// The server's defaults (backend/app/services/sandbox/capacity.py: Allocation).
export const SANDBOX_ALLOCATION_DEFAULTS: SandboxAllocation = {
  cpu_overcommit: 4, memory_overcommit: 1, reserve_memory_mb: 1024, reserve_disk_mb: 5120,
}
export const SANDBOX_VOLUME_NAME = /^[a-z0-9][a-z0-9_-]{0,39}$/

// POST /computes/{id}/evacuate
export interface SandboxEvacuation {
  moved: string[]
  left: Array<{ sandbox_id: string; report_id: string; reason: string }>
}

export interface SandboxMount { volume: string; path: string; read_only: boolean; shared: boolean }
export type SandboxConfirmRuns = 'never' | 'always' | 'full_network' | 'packages'

export interface SandboxSchemaProperty {
  title?: string
  description?: string
  type?: string
  anyOf?: Array<{ type?: string }>
  default?: any
  readOnly?: boolean
  secret?: boolean
  multiline?: boolean
  transient?: boolean
  minimum?: number
  maximum?: number
}

export interface SandboxFormSchema {
  properties?: Record<string, SandboxSchemaProperty>
  required?: string[]
}

export interface SandboxComputeType {
  name: string
  title: string
  description?: string
  config_schema: SandboxFormSchema
  credentials_schema: SandboxFormSchema
  capabilities: string[]
}

export interface SandboxAccessGrant {
  principal_type: 'user' | 'group' | 'role'
  principal_id: string
}

export interface SandboxEnvironment {
  id: string
  name: string
  description?: string | null
  image: string
  cpus: number
  memory_mb: number
  disk_mb: number
  run_timeout_seconds: number
  network_policy: string
  allowed_domains: string[]
  require_strong_isolation: boolean
  is_default: boolean
  enabled: boolean
  access: SandboxAccessGrant[]
  warnings: string[]
  // Hooks: once at creation; on every start; before a stop or delete.
  setup_script?: string | null
  start_script?: string | null
  stop_script?: string | null
  // Creations that failed in a row because a hook failed, and the last error.
  hook_failures?: number
  hook_last_error?: string | null
  compute_labels?: string[]
  confirm_runs?: SandboxConfirmRuns | string
  mounts?: SandboxMount[]
}

// After this many failed creations in a row the server refuses new sandboxes
// from the environment (backend: environments.HOOK_FAILURE_CAP).
export const SANDBOX_HOOK_FAILURE_CAP = 3

// A role, group or user an environment can be granted to.
export interface SandboxPrincipalOption { id: string; label: string; hint?: string }

// One labelled value with an optional bar (components/sandbox/SandboxBars.vue).
export interface SandboxBarLine { key: string; label: string; text: string; ratio: number | null; title?: string }

// Live usage of one sandbox, where its server can report it.
export interface SandboxUsage {
  cpu_percent?: number | null
  memory_bytes?: number | null
  memory_limit_bytes?: number | null
  disk_bytes?: number | null
  disk_limit_bytes?: number | null
}

export type SandboxState = 'provisioning' | 'ready' | 'busy' | 'idle' | 'stopped' | 'failed'

// One row of the admin's Sandboxes list (GET /sandbox/sandboxes).
export interface SandboxFleetRow {
  id: string
  report_id: string
  report_title?: string | null
  user_name?: string | null
  compute_name?: string | null
  environment_name?: string | null
  image_digest?: string | null
  state: SandboxState | string
  limits: Record<string, any>
  created_at?: string | null
  last_used_at?: string | null
  stopped_at?: string | null
  expires_at?: string | null
  error?: string | null
  // The run in progress, if any.
  running_run_id?: string | null
  running_since?: string | null
  usage?: SandboxUsage | null
  // Set up by an older image or setup script than the environment has now.
  setup_outdated?: boolean
  // Kept by an admin: retention and lifetime deletions skip it.
  pinned?: boolean
  // When it will be deleted on its own and why ("expired", "stopped too long").
  deletes_at?: string | null
  delete_reason?: string | null
  keep_warm_until?: string | null
  // Its last workspace snapshot, when there is one.
  snapshot_at?: string | null
  snapshot_bytes?: number | null
}

export interface SandboxRun {
  id: string
  status: string
  exit_code?: number | null
  language?: string | null
  // All four are null for a viewer without `view_code`.
  code?: string | null
  command?: string[] | null
  stdout?: string | null
  stderr?: string | null
  artifact_file_ids: string[]
  started_at?: string | null
  finished_at?: string | null
}

export interface SandboxEvent {
  id: string
  type: string
  details: Record<string, any>
  created_at?: string | null
}

// A domain a sandbox reached or was refused.
export interface SandboxEgress {
  domain: string
  decision: 'allowed' | 'refused' | string
  count: number
  first_at?: string | null
  last_at?: string | null
}

export interface SandboxDetail extends SandboxFleetRow {
  runs: SandboxRun[]
  events: SandboxEvent[]
  // Refused first. Collected every couple of minutes, so it can lag.
  egress?: SandboxEgress[]
}

export function useSandboxApi() {
  async function call<T = any>(path: string, opts: Record<string, any> = {}): Promise<T> {
    const { data, error } = await useMyFetch(`/sandbox${path}`, opts as any)
    if (error.value) throw error.value
    return data.value as T
  }
  return { call }
}

// The declared type of a JSON-schema property; an optional field is
// `anyOf: [{type}, {type: 'null'}]`.
export function sandboxFieldType(prop: SandboxSchemaProperty): string {
  if (prop.type) return prop.type
  return prop.anyOf?.find(option => option.type && option.type !== 'null')?.type || 'string'
}

// The API's timestamps are UTC without a zone suffix.
export function parseSandboxDate(value: string): Date {
  return new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`)
}
