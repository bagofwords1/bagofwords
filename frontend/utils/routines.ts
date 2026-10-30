// Stored procedures and functions are indexed as schema entries marked
// metadata_json.type === 'routine' (backend: app/data_sources/routines.py).
// They are listed and selected like tables, but they are not relations:
// their "columns" are parameters and they carry their source code.

export type RoutineBlock = {
  kind?: 'procedure' | 'function'
  signatures?: string[]
  definition?: string
  definition_available?: boolean
  definition_truncated?: boolean
  returns?: string
  language?: string
  overloads?: number
}

type WithMeta = { metadata_json?: Record<string, any> | null } | null | undefined

export const isRoutine = (t: WithMeta): boolean => t?.metadata_json?.type === 'routine'

export const routineMeta = (t: WithMeta): RoutineBlock =>
  (isRoutine(t) && t?.metadata_json?.routine) || {}

export const routineKind = (t: WithMeta): 'procedure' | 'function' | null =>
  isRoutine(t) ? (routineMeta(t).kind || 'procedure') : null

export const ROUTINE_ICON = 'i-heroicons-command-line'
