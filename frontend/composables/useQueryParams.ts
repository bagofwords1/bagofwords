/**
 * Query parameter helpers shared by every editor that declares ParamSpecs
 * next to code: the report's query editor (QueryCodeEditorModal) and the
 * catalog's saved-query form (EntityEditModal). Pure functions over the
 * ParamSpec shape in backend/app/schemas/param_schema.py — no state.
 */

export type ParamSpecDraft = {
  name: string
  type?: string
  label?: string | null
  source?: 'input' | 'identity' | 'input_identity_default' | string
  default?: any
  required?: boolean
  identity_binding?: string | null
  options?: any[] | null
  options_source?: { query_id: string; value_column: string; label_column?: string | null } | null
  [key: string]: any
}

export const PARAM_TYPES = ['string', 'number', 'date', 'date_range', 'id', 'list'] as const

export function newParamSpec(): ParamSpecDraft {
  return {
    name: '', type: 'string', label: '', source: 'input',
    default: null, required: false, identity_binding: 'viewer.email',
    options: null, options_source: null,
  }
}

/** Input params (the ones a test value applies to); identity ones are bound server-side. */
export function editableParamSpecs(specs: ParamSpecDraft[]): ParamSpecDraft[] {
  return (specs || []).filter(s => s && s.source !== 'identity' && s.name)
}

/**
 * Does the code treat NULL for this param as "All"? Heuristics for the SQL
 * optional pattern and the python guard — drives honest placeholders and the
 * 0-rows hint (a strict `= :name` with NULL matches nothing).
 */
export function usesOptionalPattern(code: string, name: string): boolean {
  if (!name) return false
  const src = code || ''
  try {
    return new RegExp(':' + name + '\\s+is\\s+null', 'i').test(src)
      || new RegExp("params\\.get\\((['\"])" + name + "\\1\\)").test(src)
      || new RegExp('\\b' + name + '\\b\\s+is\\s+None').test(src)
  } catch { return false }
}

export function testValuePlaceholder(code: string, spec: ParamSpecDraft): string {
  if (spec.default != null && spec.default !== '') return String(spec.default)
  return usesOptionalPattern(code, spec.name) ? '(All)' : 'empty = no rows'
}

export function parseParamOptions(raw: string): string[] | null {
  const items = raw.split(',').map(s => s.trim()).filter(Boolean)
  return items.length ? items : null
}

/** The declarations as the backend stores them: named rows, normalized fields. */
export function cleanParamSpecs(specs: ParamSpecDraft[]): any[] {
  return (specs || [])
    .filter(s => s && s.name)
    .map(s => ({
      name: s.name,
      type: s.type || 'string',
      label: s.label || null,
      source: s.source || 'input',
      default: s.default === '' ? null : s.default,
      required: !!s.required,
      identity_binding: (s.source === 'identity' || s.source === 'input_identity_default')
        ? (s.identity_binding || 'viewer.email') : null,
      options: Array.isArray(s.options) && s.options.length ? s.options : null,
      options_source: (s.options_source && s.options_source.query_id && s.options_source.value_column)
        ? {
            query_id: s.options_source.query_id,
            value_column: s.options_source.value_column,
            label_column: s.options_source.label_column || null,
          }
        : null,
    }))
}

/** Test values for the input params that have one (empty = let the backend resolve the default). */
export function collectTestValues(specs: ParamSpecDraft[], values: Record<string, any>): Record<string, any> {
  const out: Record<string, any> = {}
  for (const spec of editableParamSpecs(specs)) {
    const v = (values || {})[spec.name]
    if (v !== undefined && v !== '') out[spec.name] = v
  }
  return out
}

/**
 * A strict-equality param that resolved to NULL explains a 0-row result
 * better than the empty table does. Null when there is nothing to say.
 */
export function zeroRowsHint(
  rows: any[] | null | undefined,
  applied: Record<string, any> | null | undefined,
  specs: ParamSpecDraft[],
  code: string,
): string | null {
  if (!Array.isArray(rows) || rows.length !== 0) return null
  const values = applied || {}
  const culprit = editableParamSpecs(specs).find(s =>
    (values[s.name] === null || values[s.name] === undefined)
    && Object.prototype.hasOwnProperty.call(values, s.name)
    && !usesOptionalPattern(code, s.name))
  if (!culprit) return null
  return `'${culprit.name}' was empty and resolved to NULL — a strict '= :${culprit.name}' filter matches nothing. ` +
    `Set a test value, or use the '(:${culprit.name} IS NULL OR col = :${culprit.name})' pattern for an All behavior.`
}
