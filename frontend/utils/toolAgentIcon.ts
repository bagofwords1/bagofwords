import type { ComputedRef, InjectionKey } from 'vue'

export interface ToolAgent {
  id: string
  icon_token?: string | null
  icon?: string | null
  type?: string | null
  connector_key?: string | null
  connections?: { id: string; name?: string }[]
}

export interface AgentToolExecution {
  data_sources?: ToolAgent[] | null
  arguments_json?: Record<string, any> | null
  result_json?: Record<string, any> | null
}

// undefined = a nested tool row, null = the composer's generic agent icon.
export const toolAgentIconKey: InjectionKey<ComputedRef<ToolAgent | null | undefined>> = Symbol('tool-agent-icon')

export function resolveToolAgent(executions: AgentToolExecution[], agents: ToolAgent[] = []): ToolAgent | null {
  const owners = new Map<string, ToolAgent>()
  for (const execution of executions) {
    const args = execution.arguments_json || {}
    const result = execution.result_json || {}
    const sources = execution.data_sources || []
    const ids = new Set([
      ...sources.map(source => source.id),
      args.data_source_id, args.agent_id, result.data_source_id, result.via_agent?.id,
      ...(Array.isArray(args.agent_ids) ? args.agent_ids : []),
      ...(Array.isArray(result.top_tables) ? result.top_tables.map(table => table?.data_source_id) : []),
      ...(Array.isArray(args.tables_by_source) ? args.tables_by_source.map(row => row?.data_source_id) : []),
      ...(Array.isArray(args.data_source_ids) ? args.data_source_ids : []),
      ...(Array.isArray(result.data_source_ids) ? result.data_source_ids : []),
    ].filter(Boolean))
    for (const id of ids) {
      const owner = sources.find(source => source.id === id) || agents.find(agent => agent.id === id)
      if (owner) owners.set(id, owner)
      else if (id === 'builtin:bow') owners.set(id, { id, icon_token: 'type:bow' })
      else return null
    }
    if (ids.size) continue

    const connections = new Set([result.connection_id || result.connection_name || args.connection_id || args.connection_name,
      ...(Array.isArray(args.connection_ids) ? args.connection_ids : []),
    ].filter(Boolean))
    if (!connections.size) return null
    for (const connection of connections) {
      const matches = agents.filter(agent => agent.connections?.some(c => c.id === connection || c.name === connection))
      if (matches.length !== 1) return null
      owners.set(matches[0].id, matches[0])
    }
  }
  return owners.size === 1 ? owners.values().next().value ?? null : null
}
