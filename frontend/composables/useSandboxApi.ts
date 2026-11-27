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

export interface SandboxBackend {
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
}

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

export interface SandboxBackendType {
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
}

// A role, group or user an environment can be granted to.
export interface SandboxPrincipalOption { id: string; label: string; hint?: string }

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
  backend_name?: string | null
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

export interface SandboxDetail extends SandboxFleetRow {
  runs: SandboxRun[]
  events: SandboxEvent[]
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
