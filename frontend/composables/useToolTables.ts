// Shared table-list display logic for data tools (InspectDataTool,
// CreateDataTool). Groups the planner's `tables_by_source` argument by
// connection type and truncates long lists for the one-line ticker —
// "applications, tiers +6" instead of ten wrapping names. The full list
// stays reachable via the group's `title` tooltip.

export interface ToolTableGroup {
  type: string
  names: string[]      // full list (tooltip / expanded views)
  visible: string[]    // first MAX_VISIBLE_TABLES names for the ticker line
  more: number         // how many names were truncated (0 = none)
  title: string        // full comma-joined list for :title
}

export const MAX_VISIBLE_TABLES = 3

interface DataSourceLike {
  id: string
  type?: string
  data_source_type?: string
  connections?: Array<{ id: string; type: string }>
}

export function groupToolTables(
  argumentsJson: any,
  dataSources?: DataSourceLike[],
  maxVisible: number = MAX_VISIBLE_TABLES,
): ToolTableGroup[] {
  const aj = argumentsJson || {}
  if (!Array.isArray(aj.tables_by_source)) return []

  const groups: Record<string, string[]> = {}
  for (const group of aj.tables_by_source) {
    let connType = 'resource'
    if (group.data_source_id && dataSources?.length) {
      const ds = dataSources.find((d) => d.id === group.data_source_id)
      if (ds) {
        connType = ds.connections?.[0]?.type || ds.type || ds.data_source_type || 'resource'
      }
    }
    if (!groups[connType]) groups[connType] = []
    if (Array.isArray(group.tables)) {
      groups[connType].push(...group.tables)
    }
  }

  return Object.entries(groups).map(([type, names]) => ({
    type,
    names,
    // Truncate per source group. When exactly one name would be hidden,
    // just show it — "+1" costs the same width as the name it hides.
    visible: names.length > maxVisible + 1 ? names.slice(0, maxVisible) : names,
    more: names.length > maxVisible + 1 ? names.length - maxVisible : 0,
    title: names.join(', '),
  }))
}
