/** Declared references and conservative, explicitly labelled key-name suggestions. */
export type GraphTable = {
  id?: string; name: string; is_active: boolean; connection_id?: string; connection_name?: string; connection_type?: string;
  columns?: { name: string; dtype?: string; type?: string }[];
  pks?: { name: string }[];
  fks?: { column?: { name: string }; references_name: string; references_column?: { name: string } }[];
  metadata_json?: { schema?: string; [key: string]: any };
  centrality_score?: number; usage_count?: number; last_used_at?: string; pos_feedback_count?: number; neg_feedback_count?: number;
  success_count?: number; failure_count?: number; custom_query_id?: string;
  last_refreshed_at?: string; last_refresh_status?: string; rls_enabled?: boolean;
}
export const tableId = (t: GraphTable) => t.id || `${t.connection_id || ''}:${t.metadata_json?.schema || ''}:${t.name}`
export type TableLink = { id: string; source: string; target: string; suggested?: boolean; columns: { from: string; to: string }[] }
const clean = (s: string) => s.replace(/["`\[\]]/g, '')
const normalized = (s: string) => clean(s).replace(/([a-z0-9])([A-Z])/g, '$1_$2').toLowerCase()
const entityName = (s: string) => normalized(s.split('.').pop() || s).replace(/ies$/, 'y').replace(/(ches|shes|xes|zes|sses)$/, match => match.slice(0, -2)).replace(/(?<!s)s$/, '')
function typeFamily(type?: string) {
  if (!type) return ''
  if (/int|number|numeric|decimal|float|double|real/i.test(type)) return 'number'
  if (/char|text|string|uuid/i.test(type)) return 'text'
  return type.toLowerCase()
}
export function tableGraph(tables: GraphTable[]) {
  const index = new Map<string, GraphTable[]>()
  const add = (key: string, t: GraphTable) => {
    const rows = index.get(key) || []
    if (!rows.includes(t)) rows.push(t)
    index.set(key, rows)
  }
  for (const t of tables) {
    const prefix = `${t.connection_id || ''}|`
    const name = clean(t.name)
    add(prefix + name, t)
    if (t.metadata_json?.schema && !name.includes('.')) add(prefix + t.metadata_json.schema + '.' + name, t)
  }
  const links = new Map<string, TableLink>()
  const neighbors = new Map(tables.map(t => [tableId(t), new Set<string>()]))
  const unresolved = new Map<string, number>()
  for (const t of tables) {
    const source = tableId(t)
    for (const fk of t.fks || []) {
      if (!fk.references_name) continue
      const ref = clean(fk.references_name)
      const prefix = `${t.connection_id || ''}|`
      const schema = t.metadata_json?.schema || (clean(t.name).includes('.') ? clean(t.name).split('.').slice(0, -1).join('.') : '')
      const scoped = !ref.includes('.') && schema ? index.get(prefix + schema + '.' + ref) : undefined
      const candidates = scoped || index.get(prefix + ref) || []
      if (candidates.length !== 1) { unresolved.set(source, (unresolved.get(source) || 0) + 1); continue }
      const target = tableId(candidates[0])
      const id = JSON.stringify([source, target])
      const link = links.get(id) || { id, source, target, columns: [] }
      const pair = { from: fk.column?.name || '', to: fk.references_column?.name || '' }
      if (!link.columns.some(c => c.from === pair.from && c.to === pair.to)) link.columns.push(pair)
      links.set(id, link)
      neighbors.get(source)!.add(target)
      neighbors.get(target)!.add(source)
    }
  }
  // Index unique identity keys, rather than comparing every pair of tables.
  // A bare shared `id` never establishes a relationship. Ambiguity and known
  // incompatible types suppress suggestions; declared references take priority.
  const entities = new Map<string, { table: GraphTable; key: string; type: string }[]>()
  for (const table of tables) {
    const entity = entityName(table.name)
    const keys = table.pks?.length ? table.pks.map(k => k.name) : (table.columns || []).filter(c => ['id', `${entity}_id`, `${entity}_key`, `${entity}_uuid`].includes(normalized(c.name))).map(c => c.name)
    if (keys.length !== 1) continue
    const column = table.columns?.find(c => c.name === keys[0])
    const candidates = entities.get(entity) || []
    candidates.push({ table, key: keys[0], type: typeFamily(column?.dtype || column?.type) })
    entities.set(entity, candidates)
  }
  for (const table of tables) {
    const declaredColumns = new Set((table.fks || []).map(fk => fk.column?.name))
    for (const column of table.columns || []) {
      if (declaredColumns.has(column.name)) continue
      const match = normalized(column.name).match(/^(.+)_(?:id|key|uuid)$/)
      if (!match) continue
      const family = typeFamily(column.dtype || column.type)
      const candidates = (entities.get(entityName(match[1])) || []).filter(candidate => tableId(candidate.table) !== tableId(table) && (!family || !candidate.type || family === candidate.type))
      const local = candidates.filter(candidate => candidate.table.connection_id === table.connection_id)
      const scoped = local.filter(candidate => candidate.table.metadata_json?.schema === table.metadata_json?.schema)
      const targets = scoped.length ? scoped : local.length ? local : candidates
      if (targets.length !== 1) continue
      const source = tableId(table), target = tableId(targets[0].table)
      const id = JSON.stringify([source, target, 'suggested'])
      const link = links.get(id) || { id, source, target, suggested: true, columns: [] }
      link.columns.push({ from: column.name, to: targets[0].key })
      links.set(id, link)
      neighbors.get(source)!.add(target)
      neighbors.get(target)!.add(source)
    }
  }
  return { links: [...links.values()], neighbors, unresolved }
}
export function visibleTableIds(selected: Set<string>, neighbors: Map<string, Set<string>>, explored: Set<string>) {
  const visible = new Set(explored)
  for (const id of selected) {
    visible.add(id)
    for (const neighbor of neighbors.get(id) || []) visible.add(neighbor)
  }
  return visible
}
export function matchesTable(t: GraphTable, filter: Record<string, any> | null, active = t.is_active) {
  if (!filter) return true
  if (filter.connection?.length && !filter.connection.includes(t.connection_id)) return false
  if (filter.schema?.length && !filter.schema.some((s: string) => s === t.metadata_json?.schema || s === `${t.connection_name}:${t.metadata_json?.schema}`)) return false
  // Match the existing backend's SQL LIKE search semantics, including % and _.
  if (filter.search) {
    const escaped = String(filter.search).trim().replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/%/g, '.*').replace(/_/g, '.')
    if (!new RegExp(escaped, 'i').test(t.name)) return false
  }
  if (filter.selected_state === 'selected' && !active) return false
  if (filter.selected_state === 'unselected' && active) return false
  return true
}
