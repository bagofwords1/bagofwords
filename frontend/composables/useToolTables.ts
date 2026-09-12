// Shared table-list display logic for data tools (InspectDataTool,
// CreateDataTool). Groups the planner's `tables_by_source` argument by the
// agent's icon and truncates long lists for the one-line ticker —
// "applications, tiers +6" instead of ten wrapping names. The full list
// stays reachable via the group's `title` tooltip.
//
// The icon is NOT derived here. It arrives pre-resolved as `icon_token` on the
// agent payload (backend/app/schemas/agent_icon.py) and is passed straight to
// DataSourceIcon, so the ticker shows the same icon the agents explorer shows.
// This file used to reduce an agent to `connections[0].type`, which is why a
// multi-connection agent got one icon here and a different one everywhere else.

export interface ToolTableGroup {
  // Pre-resolved agent icon ("emoji:<grapheme>" | "type:<key>"), for
  // <DataSourceIcon :icon-token="…">. Null when the agent is unknown to this
  // payload — the caller renders its own generic glyph.
  iconToken: string | null
  // Bag of Words' own training data rather than one of the org's agents — the
  // ticker labels that group differently.
  isBow: boolean
  names: string[]      // full list (tooltip / expanded views)
  visible: string[]    // first MAX_VISIBLE_TABLES names for the ticker line
  more: number         // how many names were truncated (0 = none)
  title: string        // full comma-joined list for :title
  moreTitle: string    // the truncated names — hover tooltip for the "+N" chip
}

export const MAX_VISIBLE_TABLES = 3

// Bag of Words' own training data is a synthetic source with no agent row.
const BOW_SOURCE_ID = 'builtin:bow'
const BOW_ICON_TOKEN = 'type:bow'
// An agent the payload doesn't describe (dropped from the report, or a view
// that resolved no agents at all).
const UNKNOWN_ICON_TOKEN = 'type:resource'

/** Any payload shape that names an agent and carries its resolved icon. */
export interface AgentIconSource {
  id: string
  icon_token?: string | null
}

export function groupToolTables(
  argumentsJson: any,
  dataSources?: AgentIconSource[] | null,
  maxVisible: number = MAX_VISIBLE_TABLES,
): ToolTableGroup[] {
  const aj = argumentsJson || {}
  if (!Array.isArray(aj.tables_by_source)) return []

  const groups: Record<string, string[]> = {}
  for (const group of aj.tables_by_source) {
    let token = group.data_source_id === BOW_SOURCE_ID ? BOW_ICON_TOKEN : UNKNOWN_ICON_TOKEN
    if (group.data_source_id && dataSources?.length) {
      const ds = dataSources.find((d) => d.id === group.data_source_id)
      if (ds?.icon_token) {
        token = ds.icon_token
      }
    }
    if (!groups[token]) groups[token] = []
    if (Array.isArray(group.tables)) {
      groups[token].push(...group.tables)
    }
  }

  return Object.entries(groups).map(([iconToken, names]) => {
    // Truncate per source group. When exactly one name would be hidden,
    // just show it — "+1" costs the same width as the name it hides.
    const truncate = names.length > maxVisible + 1
    const visible = truncate ? names.slice(0, maxVisible) : names
    return {
      iconToken,
      isBow: iconToken === BOW_ICON_TOKEN,
      names,
      visible,
      more: truncate ? names.length - maxVisible : 0,
      title: names.join(', '),
      moreTitle: truncate ? names.slice(maxVisible).join(', ') : '',
    }
  })
}
