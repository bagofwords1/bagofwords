// Suggestions for the query editor, from a connection's catalog.
//
// Deliberately not a SQL parser: suggestions only need to know where the
// cursor is (after FROM/JOIN, after `x.`, anywhere else) and which tables the
// query already names, and a few patterns answer both for every dialect the
// editor serves. A wrong guess costs an unhelpful list, never a wrong query.
//
// Every name carries `insert`, the text that means exactly that name in the
// source's dialect (quoted only when needed), built by the backend
// (GET /connections/{id}/completions).

export type SqlCatalogColumn = { name: string; insert: string; dtype?: string | null }
export type SqlCatalogTable = { name: string; insert: string; columns: SqlCatalogColumn[] }
export type SqlCatalog = { language: string; tables: SqlCatalogTable[] }

export type SqlCursorContext =
  | { kind: 'table' }
  | { kind: 'member'; qualifier: string }
  | { kind: 'any' }

export type SqlSuggestion = {
  label: string
  insert: string
  kind: 'table' | 'column' | 'keyword'
  detail?: string
  // Lower sorts first: the query's own tables' columns, then tables, then keywords.
  rank: number
}

// One identifier, bare or quoted in any of the three quote styles.
const IDENT = String.raw`(?:[A-Za-z_][\w$]*|"(?:[^"]|"")+"|\x60(?:[^\x60]|\x60\x60)+\x60|\[(?:[^\]]|\]\])+\])`
const DOTTED = String.raw`${IDENT}(?:\s*\.\s*${IDENT})*`

// Words that can follow a table reference without being its alias.
const NOT_ALIAS = new Set([
  'where', 'join', 'inner', 'left', 'right', 'full', 'outer', 'cross', 'on', 'using',
  'group', 'order', 'limit', 'offset', 'having', 'union', 'except', 'intersect',
  'window', 'qualify', 'fetch', 'natural', 'lateral', 'as', 'with', 'tablesample',
])

export const SQL_KEYWORDS = [
  'SELECT', 'FROM', 'WHERE', 'JOIN', 'LEFT JOIN', 'INNER JOIN', 'ON', 'AS', 'AND', 'OR',
  'NOT', 'IN', 'IS NULL', 'IS NOT NULL', 'LIKE', 'BETWEEN', 'GROUP BY', 'ORDER BY',
  'HAVING', 'LIMIT', 'DISTINCT', 'COUNT', 'SUM', 'AVG', 'MIN', 'MAX', 'CASE', 'WHEN',
  'THEN', 'ELSE', 'END', 'UNION ALL', 'WITH', 'DESC', 'ASC', 'CAST', 'COALESCE',
]

export function unquoteIdent(part: string): string {
  const p = part.trim()
  if (p.length >= 2) {
    const [a, b] = [p[0], p[p.length - 1]]
    if (a === '"' && b === '"') return p.slice(1, -1).replace(/""/g, '"')
    if (a === '`' && b === '`') return p.slice(1, -1).replace(/``/g, '`')
    if (a === '[' && b === ']') return p.slice(1, -1).replace(/\]\]/g, ']')
  }
  return p
}

function splitDotted(ref: string): string[] {
  const parts: string[] = []
  const re = new RegExp(IDENT, 'g')
  let m: RegExpExecArray | null
  while ((m = re.exec(ref))) parts.push(unquoteIdent(m[0]))
  return parts
}

/** Where the cursor is, given the text before it minus the word being typed. */
export function sqlCursorContext(textBeforeWord: string): SqlCursorContext {
  const member = textBeforeWord.match(new RegExp(String.raw`(${IDENT})\s*\.\s*$`))
  if (member) return { kind: 'member', qualifier: unquoteIdent(member[1]) }
  if (/\b(from|join|into|update)\s+$/i.test(textBeforeWord)) return { kind: 'table' }
  // `FROM a, |` — another table in a comma-separated FROM list.
  if (new RegExp(String.raw`\bfrom\s+${DOTTED}(?:\s+(?:as\s+)?[A-Za-z_][\w$]*)?(?:\s*,\s*${DOTTED}(?:\s+(?:as\s+)?[A-Za-z_][\w$]*)?)*\s*,\s*$`, 'i').test(textBeforeWord)) {
    return { kind: 'table' }
  }
  return { kind: 'any' }
}

/** Find the catalog table a written reference means. */
export function resolveCatalogTable(parts: string[], catalog: SqlCatalog): SqlCatalogTable | null {
  if (!parts.length) return null
  const full = parts.join('.').toLowerCase()
  const last = parts[parts.length - 1].toLowerCase()
  let byLast: SqlCatalogTable | null = null
  for (const t of catalog.tables) {
    const name = t.name.toLowerCase()
    if (name === full) return t
    if (!byLast && (name === last || name.endsWith(`.${last}`))) byLast = t
  }
  return byLast
}

/** Tables the query names, keyed by every way it can refer to them
 *  (alias, bare name, full name), lower-cased. */
export function sqlReferencedTables(sql: string, catalog: SqlCatalog): Map<string, SqlCatalogTable> {
  const out = new Map<string, SqlCatalogTable>()
  const re = new RegExp(String.raw`\b(?:from|join)\s+(${DOTTED})(?:\s+(?:as\s+)?([A-Za-z_][\w$]*))?|,\s*(${DOTTED})(?:\s+(?:as\s+)?([A-Za-z_][\w$]*))?`, 'gi')
  let m: RegExpExecArray | null
  while ((m = re.exec(sql))) {
    const ref = m[1] || m[3]
    const alias = m[2] || m[4]
    const parts = splitDotted(ref)
    const table = resolveCatalogTable(parts, catalog)
    if (!table) continue
    out.set(parts.join('.').toLowerCase(), table)
    out.set(parts[parts.length - 1].toLowerCase(), table)
    if (alias && !NOT_ALIAS.has(alias.toLowerCase())) out.set(alias.toLowerCase(), table)
  }
  return out
}

function columnItems(t: SqlCatalogTable, rank: number): SqlSuggestion[] {
  return t.columns.map(c => ({
    label: c.name,
    insert: c.insert,
    kind: 'column' as const,
    detail: [c.dtype, t.name].filter(Boolean).join(' · '),
    rank,
  }))
}

function tableItems(catalog: SqlCatalog, rank: number): SqlSuggestion[] {
  return catalog.tables.map(t => ({
    label: t.name,
    insert: t.insert,
    kind: 'table' as const,
    detail: `${t.columns.length} columns`,
    rank,
  }))
}

/** The suggestion list for a cursor position. */
export function sqlSuggestions(ctx: SqlCursorContext, sql: string, catalog: SqlCatalog | null): SqlSuggestion[] {
  const keywords = (rank: number) => SQL_KEYWORDS.map(k => ({ label: k, insert: k, kind: 'keyword' as const, rank }))
  if (!catalog) return ctx.kind === 'member' ? [] : keywords(2)

  if (ctx.kind === 'table') return tableItems(catalog, 0)

  const refs = sqlReferencedTables(sql, catalog)
  if (ctx.kind === 'member') {
    const t = refs.get(ctx.qualifier.toLowerCase()) || resolveCatalogTable([ctx.qualifier], catalog)
    return t ? columnItems(t, 0) : []
  }

  // Anywhere else: the columns of the tables this query already uses first.
  const seen = new Set<SqlCatalogTable>()
  const cols: SqlSuggestion[] = []
  for (const t of refs.values()) {
    if (seen.has(t)) continue
    seen.add(t)
    cols.push(...columnItems(t, 0))
  }
  return [...cols, ...tableItems(catalog, 1), ...keywords(2)]
}
