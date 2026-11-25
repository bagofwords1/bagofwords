// What a cached (custom) table's column says about itself, read off the
// `metadata` the backend stores on each column
// (backend/app/data_sources/fast/column_metadata.py):
//
//   source           where it was copied from, or the expression that computes it
//   additive         true | false | 'unknown': may its values be summed across rows
//   additive_reason  why (a key under cachedColumns.reasons.*)
//   edited           fields an admin set by hand; a refresh leaves them alone
//   inherited        what the source says for those fields
//   drift            set when the source definition changed under the copy
//
// Pure functions only, shared by the custom-table dialog and the tables page
// and covered by tests/unit/columnMetadata.mjs.

export type Additive = true | false | 'unknown'
export type AdditiveState = 'additive' | 'notAdditive' | 'unknown'

export interface ColumnEdit {
  description?: string
  additive?: Additive
}

export interface ColumnSourceLabel {
  // 'column' | 'measure' | 'derived'
  kind: string
  // The source column (`table.column`) or the computing expression.
  text: string
  // Source columns a computed expression reads.
  reads: string[]
}

const meta = (col: any): Record<string, any> =>
  (col && typeof col.metadata === 'object' && col.metadata) || {}

/** Whether the column carries any of the inherited metadata at all. */
export function hasColumnMetadata(col: any): boolean {
  const m = meta(col)
  return 'additive' in m || !!m.source || !!col?.description
}

export function additiveState(col: any): AdditiveState | null {
  const m = meta(col)
  if (!('additive' in m)) return null
  if (m.additive === true) return 'additive'
  if (m.additive === false) return 'notAdditive'
  return 'unknown'
}

/** The reason key to show beside the flag, or null when it adds nothing. */
export function additiveReason(col: any): string | null {
  const reason = meta(col).additive_reason
  return typeof reason === 'string' && reason ? reason : null
}

/**
 * Whether the flag is worth showing on a read-only list. A text or date
 * column, a grouping key and an identifier are "not additive" in a way nobody
 * needs telling; the flag is for numbers someone might add up.
 */
export function showsAdditivity(col: any): boolean {
  const state = additiveState(col)
  if (!state) return false
  if (state !== 'notAdditive') return true
  return !['not_numeric', 'grouping_key', 'key'].includes(additiveReason(col) || '')
}

export function sourceLabel(col: any): ColumnSourceLabel | null {
  const source = meta(col).source
  if (!source || typeof source !== 'object') return null
  const reads = Array.isArray(source.reads)
    ? source.reads.filter((r: any) => r?.table && r?.column).map((r: any) => `${r.table}.${r.column}`)
    : []
  if (source.kind === 'derived') {
    return source.expression ? { kind: 'derived', text: String(source.expression), reads } : null
  }
  if (!source.table || !source.column) return null
  return { kind: String(source.kind || 'column'), text: `${source.table}.${source.column}`, reads }
}

/**
 * Whether to show the origin line under a description. A computed column's
 * inherited description already begins with its expression, so repeating the
 * expression beneath it says nothing; once the description is the admin's
 * own words (or a plain copy), the origin is the only place it is stated.
 */
export function showsSource(col: any, description: string | null | undefined): boolean {
  const label = sourceLabel(col)
  if (!label) return false
  if (label.kind !== 'derived') return true
  return !String(description || '').trim().startsWith(label.text)
}

export function isEdited(col: any, field: 'description' | 'additive'): boolean {
  const edited = meta(col).edited
  return Array.isArray(edited) && edited.includes(field)
}

export function driftOf(col: any): { detectedAt: string | null; previousExpression: string | null } | null {
  const drift = meta(col).drift
  if (!drift || typeof drift !== 'object') return null
  return {
    detectedAt: drift.detected_at || null,
    previousExpression: drift.previous_expression || null,
  }
}

/** What the source says, whether or not an admin has overridden it. */
export function inheritedDescription(col: any): string {
  const m = meta(col)
  if (isEdited(col, 'description')) return String(m.inherited?.description || '')
  return String(col?.description || '')
}

export function inheritedAdditive(col: any): Additive | null {
  const m = meta(col)
  const value = isEdited(col, 'additive') ? m.inherited?.additive : m.additive
  return value === true || value === false || value === 'unknown' ? value : null
}

/** The admin's edits already stored on a saved table's columns. */
export function editsFromColumns(columns: any[]): Record<string, ColumnEdit> {
  const edits: Record<string, ColumnEdit> = {}
  for (const col of columns || []) {
    if (!col?.name) continue
    const edit: ColumnEdit = {}
    if (isEdited(col, 'description')) edit.description = String(col.description || '')
    if (isEdited(col, 'additive')) {
      const value = meta(col).additive
      if (value === true || value === false || value === 'unknown') edit.additive = value
    }
    if (Object.keys(edit).length) edits[col.name] = edit
  }
  return edits
}

/**
 * A new set of edits after the admin typed `value` into a column's field.
 * Setting a field back to what the source says removes the edit, so the
 * column goes on following the source.
 */
export function withEdit(
  edits: Record<string, ColumnEdit>, col: any, field: 'description' | 'additive', value: any,
): Record<string, ColumnEdit> {
  const next: Record<string, ColumnEdit> = { ...edits }
  const current: ColumnEdit = { ...(next[col.name] || {}) }
  const inherited = field === 'description' ? inheritedDescription(col) : inheritedAdditive(col)
  const same = field === 'description'
    ? String(value ?? '').trim() === String(inherited ?? '').trim()
    : value === inherited
  if (same || value === undefined) delete current[field]
  else if (field === 'description') current.description = String(value ?? '')
  else current.additive = value as Additive
  if (Object.keys(current).length) next[col.name] = current
  else delete next[col.name]
  return next
}

/** The value a field shows: the admin's edit, else what was inherited. */
export function shownDescription(edits: Record<string, ColumnEdit>, col: any): string {
  const edit = edits[col?.name]
  return edit && 'description' in edit ? String(edit.description ?? '') : inheritedDescription(col)
}

export function shownAdditive(edits: Record<string, ColumnEdit>, col: any): Additive | null {
  const edit = edits[col?.name]
  return edit && 'additive' in edit ? (edit.additive as Additive) : inheritedAdditive(col)
}

/**
 * The `columns` field of a create / update request: every edit that still
 * names a column of the table, plus the drift flags being marked reviewed.
 * It is the complete set — a column left out goes back to its source.
 */
export function columnEditsPayload(
  columns: any[], edits: Record<string, ColumnEdit>, reviewed: Iterable<string> = [],
): Array<Record<string, any>> {
  const reviewedSet = new Set(reviewed)
  const out: Array<Record<string, any>> = []
  for (const col of columns || []) {
    const name = col?.name
    if (!name) continue
    const edit = edits[name]
    const entry: Record<string, any> = { name }
    if (edit && 'description' in edit) entry.description = String(edit.description ?? '').trim()
    if (edit && 'additive' in edit) entry.additive = edit.additive
    if (reviewedSet.has(name)) entry.acknowledge_drift = true
    if (Object.keys(entry).length > 1) out.push(entry)
  }
  return out
}

/** Columns whose re-aggregation could not be determined and nobody has set. */
export function undecidedColumns(columns: any[], edits: Record<string, ColumnEdit> = {}): string[] {
  return (columns || [])
    .filter((col: any) => col?.name && shownAdditive(edits, col) === 'unknown')
    .map((col: any) => col.name)
}

/**
 * The description to send when saving. A description that is still exactly
 * the generated one is sent empty: the table then keeps describing itself
 * from its columns on every refresh instead of freezing today's wording.
 */
export function descriptionToSave(typed: string, generated: string | null | undefined): string {
  const text = String(typed || '').trim()
  return generated && text === String(generated).trim() ? '' : text
}

/**
 * What the description field should hold after a preview (or on opening a
 * saved table): the generated text, but only when the admin has not written
 * their own.
 */
export function descriptionAfterSuggestion(
  typed: string, previousGenerated: string | null | undefined, generated: string | null | undefined,
): string {
  const text = String(typed || '').trim()
  const untouched = !text || (!!previousGenerated && text === String(previousGenerated).trim())
  return untouched ? String(generated || '') : typed
}
