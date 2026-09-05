import assert from 'node:assert/strict'

import {
  POPULAR_DATA_SOURCE_TYPES,
  filterDataSources,
  popularDataSources,
} from '../../composables/useDataSourcePicker.ts'

// A stand-in for /available_data_sources: a few popular entries, a few that are
// only reachable through search.
const CATALOG = [
  { type: 'postgresql', title: 'PostgreSQL', category: 'databases', description: 'Postgres database' },
  { type: 'mysql', title: 'MySQL', category: 'databases', description: '' },
  { type: 'MSSQL', title: 'Microsoft SQL Server', category: 'databases', description: '' },
  { type: 'snowflake', title: 'Snowflake', category: 'databases', description: '' },
  { type: 'csv', title: 'CSV', category: 'files', description: 'Upload CSV files' },
  { type: 'zabbix', title: 'Zabbix', category: 'infra', description: 'Monitoring' },
  { type: 'qlik_sense', title: 'Qlik Sense', category: 'bi', description: '' },
  { type: 'google_drive', title: 'Google Drive', category: 'files', description: '' },
  { type: 'mcp', title: 'MCP Server', category: 'custom', description: '' },
]

// --- the collapsed view ------------------------------------------------------

const popular = popularDataSources(CATALOG)

assert.deepEqual(
  popular.map((ds) => ds.type),
  ['postgresql', 'mysql', 'MSSQL', 'snowflake', 'qlik_sense', 'mcp'],
  'the collapsed grid shows the curated types, in curated order, and nothing else',
)

// A curated type the registry doesn't offer (deprecated, or hidden by a build
// flag) must drop out rather than render an empty tile.
assert.deepEqual(popularDataSources([{ type: 'mysql', title: 'MySQL' }]).map((d) => d.type), ['mysql'])
assert.deepEqual(popularDataSources([]), [])

// Every curated type has to be spelled exactly as the registry spells it —
// "mssql" instead of "MSSQL" would silently vanish from onboarding.
assert.equal(new Set(POPULAR_DATA_SOURCE_TYPES).size, POPULAR_DATA_SOURCE_TYPES.length)

// --- search ------------------------------------------------------------------

const types = (query) => filterDataSources(CATALOG, query).map((ds) => ds.type)

assert.deepEqual(types(''), CATALOG.map((ds) => ds.type), 'an empty query matches everything')
assert.deepEqual(types('   '), CATALOG.map((ds) => ds.type), 'whitespace is not a query')

// Search spans the whole catalogue, not just the popular slice — that is the
// point of having it.
assert.deepEqual(types('zabbix'), ['zabbix'])
assert.deepEqual(types('qlik'), ['qlik_sense'])

// Case-insensitive, matches on title and on type.
assert.deepEqual(types('SNOW'), ['snowflake'])
assert.deepEqual(types('google_drive'), ['google_drive'])

// Aliases: names people actually type that aren't in the title.
assert.deepEqual(types('postgres'), ['postgresql'])
assert.deepEqual(types('excel'), ['csv'], 'csv is reachable by the format people mean')

// Multi-token queries are AND, and can span title words.
assert.deepEqual(types('sql server'), ['MSSQL'])
assert.deepEqual(types('sql zabbix'), [], 'tokens must all hit the same entry')

// --- ranking -----------------------------------------------------------------

// A description that happens to contain every token must not outrank the
// connector actually named that: "sql server" typed into onboarding means
// Microsoft SQL Server, not the warehouse whose blurb mentions a SQL server.
const RANKING = [
  { type: 'aws_athena', title: 'AWS Athena', category: 'databases', description: 'Serverless SQL over S3' },
  { type: 'MSSQL', title: 'Microsoft SQL Server', category: 'databases', description: '' },
  { type: 'databricks_sql', title: 'Databricks SQL', category: 'databases', description: 'SQL warehouse server' },
]
assert.equal(
  filterDataSources(RANKING, 'sql server')[0].type,
  'MSSQL',
  'a title match must rank above a description match',
)

// An exact title beats a title that merely contains the words.
assert.equal(filterDataSources(RANKING, 'databricks')[0].type, 'databricks_sql')

// Ranking never drops matches — it only reorders them.
assert.equal(filterDataSources(RANKING, 'sql').length, 3)

// Category is searchable, so "bi" or "files" surfaces a whole family.
assert.deepEqual(types('files'), ['csv', 'google_drive'])

assert.deepEqual(types('cassandra'), [], 'no match yields an empty list, not everything')

console.log('data source picker: popular subset + search over the full catalogue')
