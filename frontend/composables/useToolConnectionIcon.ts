// Resolve the data source / connection a tool call ran against, so a tool-row
// component can render the provider's brand icon (OneDrive, SharePoint, Gmail,
// Snowflake, …) instead of a generic glyph.
//
// A call is attributed by any identifier it carries — `connection_id` /
// `data_source_id` / `connection_name` (args or streamed result) or the plural
// `connection_ids` (search_mcps) — matched against the report's data sources.
// When nothing identifies a specific source but the agent has exactly ONE
// candidate connection, we attribute to it (covers discovery rows like
// search_mcps "Finding <provider> tools" and file tools that omit the id).
//
// Returns `DataSourceIcon` props ({ type, connectorKey }) or `null` when it
// can't resolve — callers render their existing fallback glyph on null.
import { computed, unref, type MaybeRef } from 'vue'

// File-source connection types — used to scope the sole-connection fallback for
// the file tools so a mixed (DB + file) agent doesn't misattribute.
export const FILE_SOURCE_TYPES = [
  'sharepoint', 'onedrive', 'google_drive', 'outlook_mail', 'network_dir', 's3',
]

export interface ToolIconProps {
  type: string
  connectorKey: string | null
}

interface ToolExecutionLike {
  tool_name?: string
  arguments_json?: Record<string, any> | null
  result_json?: Record<string, any> | null
}

export function useToolConnectionIcon(
  toolExecution: MaybeRef<ToolExecutionLike | null | undefined>,
  dataSources: MaybeRef<any[] | null | undefined>,
  opts: { connectionTypes?: string[] } = {},
) {
  const typeFilter = opts.connectionTypes ? new Set(opts.connectionTypes) : null

  return computed<ToolIconProps | null>(() => {
    const dss = unref(dataSources) || []
    const te = unref(toolExecution) || {}
    const args = te.arguments_json || {}
    const rj = te.result_json || {}

    // Identifiers this call may carry, normalised to lowercase strings.
    const ids = new Set<string>()
    for (const v of [args.connection_id, args.data_source_id, rj.connection_name, rj.connection_id]) {
      if (v) ids.add(String(v).toLowerCase())
    }
    for (const arr of [args.connection_ids, rj.connection_ids]) {
      if (Array.isArray(arr)) arr.forEach((x: any) => x && ids.add(String(x).toLowerCase()))
    }

    // Candidate (connection, dataSource) pairs, optionally type-scoped.
    const candidates: Array<{ c: any; ds: any }> = []
    for (const ds of dss) {
      for (const c of ds.connections || []) {
        if (typeFilter && !typeFilter.has(c.type)) continue
        candidates.push({ c, ds })
      }
    }
    if (!candidates.length) return null

    let hit: { c: any; ds: any } | undefined
    if (ids.size) {
      hit = candidates.find(({ c, ds }) =>
        ids.has(String(c.id).toLowerCase()) ||
        ids.has(String(c.name || '').toLowerCase()) ||
        ids.has(String(ds.id).toLowerCase()) ||
        ids.has(String(ds.name || '').toLowerCase()),
      )
    }
    // Sole candidate → attribute to it even without an explicit identifier.
    if (!hit && candidates.length === 1) hit = candidates[0]
    if (!hit) return null

    const c = hit.c
    return {
      type: c.type,
      connectorKey: c.connector_key || c.config?.catalog_key || null,
    }
  })
}
