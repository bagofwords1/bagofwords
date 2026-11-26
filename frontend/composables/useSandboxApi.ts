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
