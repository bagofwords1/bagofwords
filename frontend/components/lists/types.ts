export type ListFieldType = 'string' | 'number' | 'integer' | 'boolean' | 'date' | 'enum'

export interface ListField {
  id?: string
  name: string
  type: ListFieldType
  description?: string
  required?: boolean
  enum?: string[] | null
  unit?: string | null
  method?: string | null
}

export interface AgentList {
  id: string
  data_source_id: string
  name: string
  slug: string
  description?: string
  fields: ListField[]
  key_field_id?: string | null
  key_field?: string | null
  require_evidence: boolean
  allow_viewer_submissions: boolean
  version: number
  row_count: number
  tool_name: string
  table_name: string
  updated_at?: string
  can_manage: boolean
  change?: 'none' | 'additive' | 'breaking' | null
}

export interface Evidence {
  kind?: string
  ref?: string | null
  page?: number | null
  quote?: string | null
  verified?: boolean
  superseded?: boolean
}

export interface Envelope {
  value: any
  status?: 'found' | 'not_found' | 'ambiguous' | 'inferred'
  evidence?: Evidence[]
  note?: string | null
  source?: 'agent' | 'human'
  edited_by?: string
  updated_at?: string
}

export interface ListRow {
  id: string
  key_value?: string | null
  values: Record<string, Envelope>
  schema_version: number
  row_version: number
  locked_fields: string[]
  report_id?: string | null
  created_at?: string
  updated_at?: string
  stale: boolean
}

export interface Revision {
  id: string
  actor_type: 'agent' | 'user'
  actor_name?: string | null
  action: 'insert' | 'update' | 'revert' | 'unlock'
  report_id?: string | null
  changed: Record<string, any>
  created_at?: string
}

/** Stringify a stored value for display in the grid / CSV-like views. */
export function formatValue(field: ListField, value: any, t: (k: string) => string): string {
  if (value === null || value === undefined || value === '') return ''
  if (field.type === 'boolean') return value ? t('lists.row.yes') : t('lists.row.no')
  if (field.type === 'number' || field.type === 'integer') {
    const n = Number(value)
    return Number.isFinite(n) ? n.toLocaleString(undefined, { maximumFractionDigits: 4 }) : String(value)
  }
  return String(value)
}
