// /composables/useDataSourcePicker.ts
import type { Ref } from 'vue'

// The registry ships ~55 connectors, which is far too many to dump on someone
// in their first two minutes with the product. These are the ones a new
// workspace most often starts with; everything else stays one search (or one
// "show all" click) away.
export const POPULAR_DATA_SOURCE_TYPES = [
  'postgresql',
  'mysql',
  'MSSQL',
  'snowflake',
  'bigquery',
  'powerbi',
  'tableau',
  'qlik_sense',
  'aws_redshift',
  'databricks_sql',
  'sharepoint',
  'mcp',
  'custom_api',
]

// Extra search terms per type, for names people type that don't appear in the
// catalogue title (product nicknames, the vendor, the file format).
const SEARCH_ALIASES: Record<string, string[]> = {
  postgresql: ['postgres', 'pg'],
  MSSQL: ['sql server', 'microsoft', 'mssql', 'tsql'],
  bigquery: ['google', 'gcp'],
  aws_redshift: ['amazon', 'aws'],
  aws_athena: ['amazon', 'aws'],
  aws_cost: ['amazon', 'aws', 'billing'],
  s3: ['amazon', 'aws', 'bucket'],
  csv: ['excel', 'spreadsheet', 'xlsx', 'file'],
  network_dir: ['files', 'folder', 'directory', 'local', 'smb'],
  databricks_sql: ['spark', 'lakehouse'],
  azure_data_explorer: ['kusto', 'microsoft', 'adx'],
  ms_fabric: ['microsoft', 'onelake'],
  analysis_services: ['ssas', 'microsoft', 'olap'],
  powerbi: ['microsoft', 'pbi'],
  powerbi_report_server: ['microsoft', 'pbi'],
  pbix: ['powerbi', 'microsoft'],
  qvd: ['qlik'],
  google_drive: ['gdrive', 'google', 'sheets'],
  gmail_mail: ['google', 'email', 'mail'],
  outlook_mail: ['microsoft', 'email', 'mail', 'office'],
  onedrive: ['microsoft', 'office'],
  onenote: ['microsoft', 'office'],
  sharepoint: ['microsoft', 'office'],
  sharepoint_lists: ['microsoft', 'office'],
  mcp: ['model context protocol', 'tools'],
  oracledb: ['oracle'],
  mariadb: ['mysql'],
  elasticsearch: ['elastic', 'es'],
  opensearch: ['elastic'],
}

function haystack(ds: any): string {
  return [
    ds?.title,
    ds?.type,
    ds?.description,
    ds?.category,
    ...(SEARCH_ALIASES[ds?.type] || []),
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase()
}

/**
 * How well `ds` answers `query`. Higher is better; the ordering matters more
 * than the numbers. A name match outranks a description match, so "sql server"
 * puts Microsoft SQL Server first instead of burying it among the connectors
 * whose blurbs merely mention SQL and a server.
 */
function relevance(ds: any, phrase: string, tokens: string[]): number {
  const title = String(ds?.title || '').toLowerCase()
  const type = String(ds?.type || '').toLowerCase()
  const aliases = (SEARCH_ALIASES[ds?.type] || []).join(' ')
  const rest = [ds?.description, ds?.category].filter(Boolean).join(' ').toLowerCase()

  let score = 0
  if (title.startsWith(phrase)) score += 100
  else if (title.includes(phrase)) score += 60
  if (type.includes(phrase)) score += 50
  if (aliases.includes(phrase)) score += 40

  for (const token of tokens) {
    if (title.includes(token)) score += 10
    else if (type.includes(token) || aliases.includes(token)) score += 5
    else if (rest.includes(token)) score += 1
  }
  return score
}

/** The popular connectors present in `sources`, in the curated order above. */
export function popularDataSources(sources: any[]): any[] {
  const byType = new Map((sources || []).map((ds: any) => [ds.type, ds]))
  return POPULAR_DATA_SOURCE_TYPES.map((type) => byType.get(type)).filter(Boolean)
}

/**
 * Connectors matching `query`, best match first. Every token has to hit
 * somewhere in the title, type, description, category or the aliases above;
 * the surviving entries are then ordered by `relevance`. An empty query
 * matches everything, in catalogue order.
 */
export function filterDataSources(sources: any[], query: string): any[] {
  const phrase = (query || '').trim().toLowerCase().replace(/\s+/g, ' ')
  const tokens = phrase.split(' ').filter(Boolean)
  if (!tokens.length) return sources || []
  return (sources || [])
    .filter((ds: any) => {
      const text = haystack(ds)
      return tokens.every((token) => text.includes(token))
    })
    .map((ds: any) => ({ ds, score: relevance(ds, phrase, tokens) }))
    .sort((a, b) => b.score - a.score)
    .map(({ ds }) => ds)
}

// The domain categories, in the order the add-connection modal renders them,
// so both pickers name and order the catalogue the same way. `custom` (raw MCP
// / Custom API) is a chip here because onboarding has no footer to pin it to.
export const DATA_SOURCE_CATEGORIES: { key: string; label: string }[] = [
  { key: 'databases', label: 'data.catDatabases' },
  { key: 'bi', label: 'data.catBi' },
  { key: 'infra', label: 'data.catInfra' },
  { key: 'services', label: 'data.catServices' },
  { key: 'files', label: 'data.catFiles' },
  { key: 'custom', label: 'data.catCustom' },
]

/**
 * The slice of the catalogue a chip stands for: the curated set, everything,
 * or one category. Entries with no category fall in with the databases, which
 * is what the registry itself defaults them to.
 */
export function scopeDataSources(sources: any[], scope: string): any[] {
  if (scope === 'popular') return popularDataSources(sources)
  if (scope === 'all') return sources || []
  return (sources || []).filter((ds: any) => (ds?.category || 'databases') === scope)
}

/**
 * Search + category chips for the connector picker.
 *
 * The picker opens on `popular` — the curated set, not all ~68 connectors —
 * and the chips ("All" plus each non-empty category) are how you browse wider.
 * Typing resets the chip to `all` so search is global by default; picking a
 * chip afterwards narrows the results you are looking at.
 */
export function useDataSourcePicker(sources: Ref<any[]>) {
  const query = ref('')
  const activeCategory = ref('popular')

  const isSearching = computed(() => query.value.trim().length > 0)

  watch(isSearching, (searching) => {
    if (searching) activeCategory.value = 'all'
  })

  // Chips: Popular, All, then the categories the catalogue actually has.
  const chips = computed(() => {
    const present = new Set((sources.value || []).map((ds: any) => ds?.category || 'databases'))
    return [
      { key: 'popular', label: 'data.catPopular' },
      { key: 'all', label: 'data.catAll' },
      ...DATA_SOURCE_CATEGORIES.filter((c) => present.has(c.key)),
    ]
  })

  const visible = computed(() =>
    filterDataSources(scopeDataSources(sources.value, activeCategory.value), query.value),
  )

  const noResults = computed(() => isSearching.value && visible.value.length === 0)

  return { query, activeCategory, chips, isSearching, visible, noResults }
}
