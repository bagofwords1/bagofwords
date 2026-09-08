/**
 * Parser for the diagnosis query language — the TypeScript twin of
 * backend/app/services/diagnosis/grammar.py.
 *
 * Both parsers are held to the same golden fixture
 * (backend/tests/fixtures/diagnosis_queries.json): token boundaries, error
 * positions, error wording and the canonical form are a contract. Change
 * both or neither. The field registry below mirrors fields.py; an e2e test
 * compares it against GET /console/diagnosis/fields.
 */

export const AST_VERSION = 1
export const MAX_LENGTH = 1000
export const MAX_TERMS = 40
export const MAX_DEPTH = 4

export type FieldType = 'enum' | 'text' | 'number' | 'duration' | 'money' | 'date' | 'boolean' | 'id'
export type Entity = 'run' | 'tool'

export interface FieldSpec {
  name: string
  aliases: string[]
  type: FieldType
  entity: Entity
  values: string[]
  facetable: boolean
  sortable: boolean
  builder: boolean
  help: string
}

const OPS: Record<FieldType, string[]> = {
  enum: ['eq', 'any'],
  text: ['eq', 'any', 'wild'],
  number: ['eq', 'any', 'gt', 'gte', 'lt', 'lte', 'range'],
  duration: ['eq', 'any', 'gt', 'gte', 'lt', 'lte', 'range'],
  money: ['eq', 'any', 'gt', 'gte', 'lt', 'lte', 'range'],
  date: ['eq', 'gt', 'gte', 'lt', 'lte', 'range'],
  boolean: ['eq'],
  id: ['eq', 'any'],
}

function f(
  name: string, type: FieldType, entity: Entity, help: string,
  extra: Partial<Pick<FieldSpec, 'aliases' | 'values' | 'facetable' | 'sortable' | 'builder'>> = {},
): FieldSpec {
  return {
    name, type, entity, help,
    aliases: extra.aliases ?? [], values: extra.values ?? [],
    facetable: extra.facetable ?? false, sortable: extra.sortable ?? false, builder: extra.builder ?? true,
  }
}

/** Mirrors fields.FIELDS — same order, same names, same types. */
export const FIELDS: FieldSpec[] = [
  f('status', 'enum', 'run', 'Run outcome (stale = still running after an hour, i.e. never finished)', { values: ['success', 'error', 'in_progress', 'stale', 'sigkill'], facetable: true }),
  f('user', 'text', 'run', 'Who ran it (name or email)', { facetable: true }),
  f('agent', 'text', 'run', 'Agent (data source) the run drew on', { facetable: true }),
  f('platform', 'enum', 'run', 'Where the prompt came from', { values: ['web', 'slack', 'teams', 'email', 'mcp', 'api'], facetable: true }),
  f('feedback', 'enum', 'run', 'User feedback on the answer', { values: ['positive', 'negative', 'none'], facetable: true }),
  f('judge.confidence', 'number', 'run', 'Judge score for answer quality, 1–5', { aliases: ['confidence'], sortable: true }),
  f('judge.instructions', 'number', 'run', 'Judge score for instruction coverage, 1–5', { aliases: ['coverage'], sortable: true }),
  f('judge.context', 'number', 'run', 'Judge score for context use, 1–5', { sortable: true }),
  f('model', 'text', 'run', 'Planner model for the run', { facetable: true }),
  f('provider', 'text', 'run', 'LLM provider for the run', { facetable: true }),
  f('cost', 'money', 'run', 'LLM cost of the run in USD', { sortable: true }),
  f('tokens', 'number', 'run', 'LLM tokens used by the run', { sortable: true }),
  f('tokens.in', 'number', 'run', 'Prompt tokens', { sortable: true, builder: false }),
  f('tokens.out', 'number', 'run', 'Completion tokens', { sortable: true, builder: false }),
  f('duration', 'duration', 'run', 'Wall time of the run', { sortable: true }),
  f('thinking', 'duration', 'run', 'Time before the first tool call', { sortable: true, builder: false }),
  f('first_token', 'duration', 'run', 'Time to first streamed token', { sortable: true, builder: false }),
  f('tools', 'number', 'run', 'Tool calls in the run', { sortable: true }),
  f('tools.failed', 'number', 'run', 'Failed tool calls in the run', { sortable: true }),
  f('report', 'text', 'run', 'Report title'),
  f('turn', 'number', 'run', 'Position of the run in its report (1 = first prompt)', { sortable: true }),
  f('report_id', 'id', 'run', 'Report id', { builder: false }),
  f('run_id', 'id', 'run', 'Run id', { builder: false }),
  f('version', 'text', 'run', 'Bag of words version that ran it', { facetable: true, builder: false }),
  f('created', 'date', 'run', 'When the run started', { sortable: true }),
  f('eval', 'boolean', 'run', 'Runs spawned by evals (hidden unless mentioned)', { builder: false }),
  f('error', 'text', 'run', 'Run error message'),
  f('tool', 'text', 'tool', 'Tool name', { aliases: ['tool.name'], facetable: true }),
  f('tool.action', 'text', 'tool', 'Tool action', { facetable: true }),
  f('tool.status', 'enum', 'tool', 'Tool call outcome', { values: ['success', 'error', 'in_progress', 'stopped'], facetable: true }),
  f('tool.attempt', 'number', 'tool', 'Attempt number (retries start at 2)'),
  f('tool.duration', 'duration', 'tool', 'Tool call wall time'),
  f('tool.error', 'text', 'tool', 'Tool error message'),
  f('table', 'text', 'tool', 'Table the tool call queried (schema.table or table)', { facetable: true }),
  f('tool.args', 'text', 'tool', "Text anywhere in the tool call's arguments"),
  f('tool.output', 'text', 'tool', "Text in the tool call's result summary"),
]

const BY_NAME = new Map<string, FieldSpec>()
for (const spec of FIELDS) {
  BY_NAME.set(spec.name, spec)
  for (const a of spec.aliases) BY_NAME.set(a, spec)
}

export function resolveField(name: string): FieldSpec | undefined {
  return BY_NAME.get(name.toLowerCase())
}

export function fieldOps(spec: FieldSpec): string[] {
  return OPS[spec.type]
}

function levenshtein(a: string, b: string): number {
  if (a === b) return 0
  let prev = Array.from({ length: b.length + 1 }, (_, i) => i)
  for (let i = 1; i <= a.length; i++) {
    const cur = [i]
    for (let j = 1; j <= b.length; j++) {
      cur.push(Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] !== b[j - 1] ? 1 : 0)))
    }
    prev = cur
  }
  return prev[b.length]
}

/** Mirrors fields.suggest: prefix matches first, then edit distance ≤ 3, ties alphabetical. */
export function suggestFields(name: string, limit = 3): string[] {
  const low = name.toLowerCase()
  const names = Array.from(BY_NAME.keys())
  const prefixed = names.filter(n => n.startsWith(low) && n !== low).sort()
  const scored = names
    .filter(n => !prefixed.includes(n) && n !== low)
    .map(n => [levenshtein(low, n), n] as [number, string])
    .sort((x, y) => (x[0] - y[0]) || (x[1] < y[1] ? -1 : x[1] > y[1] ? 1 : 0))
  const close = scored.filter(([d]) => d <= 3).map(([, n]) => n)
  const out: string[] = []
  for (const n of [...prefixed, ...close]) {
    const canonical = BY_NAME.get(n)!.name
    if (!out.includes(canonical)) out.push(canonical)
    if (out.length >= limit) break
  }
  return out
}

// ---------------------------------------------------------------------------
// AST
// ---------------------------------------------------------------------------

export interface Literal { kind: string; raw: string; s?: string; n?: number; ms?: number; usd?: number; gran?: string; unit?: string; b?: boolean }
export type Node =
  | { t: 'and' | 'or'; c: Node[]; _pos?: number; _end?: number }
  | { t: 'not'; c: Node; _pos?: number; _end?: number }
  | { t: 'term'; field: string; op: string; values: Literal[]; _pos?: number; _end?: number }
  | { t: 'text'; phrase: boolean; s: string; _pos?: number; _end?: number }
export interface Ast { v: number; root: Node | null }
export interface Token { type: 'field' | 'colon' | 'op' | 'value' | 'keyword' | 'paren' | 'text' | 'phrase'; start: number; end: number }

export class QueryError extends Error {
  position: number
  suggestions: string[]
  constructor(position: number, message: string, suggestions: string[] = []) {
    super(message)
    this.position = position
    this.suggestions = suggestions
  }
  toDict() {
    return { position: this.position, message: this.message, suggestions: [...this.suggestions] }
  }
}

const CMP_OPS = ['>=', '<=', '>', '<']
const CMP_NAME: Record<string, string> = { '>': 'gt', '>=': 'gte', '<': 'lt', '<=': 'lte' }
const FIELD_RE = /[A-Za-z_][A-Za-z0-9_.]*/y
const NUMBER_RE = /^(\d+(?:\.\d+)?)([kKmM])?$/
const DURATION_RE = /^(\d+(?:\.\d+)?)(ms|s|m|h)?$/
const MONEY_RE = /^\$?(\d+(?:\.\d+)?)$/
const DAY_RE = /^\d{4}-\d{2}-\d{2}$/
const MONTH_RE = /^\d{4}-\d{2}$/
const REL_RE = /^-(\d+)([dh])$/

function lit(kind: string, raw: string, extra: Partial<Literal> = {}): Literal {
  return { kind, raw, ...extra }
}

const isSpace = (ch: string) => ch !== '' && /\s/.test(ch)

/** Mirrors grammar.parse_scalar. */
export function parseScalar(spec: FieldSpec, raw: string, isPhrase: boolean, position: number): Literal {
  const t = spec.type
  if (t === 'enum') {
    const low = raw.toLowerCase()
    if (!spec.values.includes(low)) {
      throw new QueryError(position, `Unknown value "${raw}" for ${spec.name}. Expected one of: ${spec.values.join(', ')}`, [...spec.values])
    }
    return lit('word', raw, { s: low })
  }
  if (t === 'text' || t === 'id') {
    if (isPhrase) return lit('phrase', raw, { s: raw })
    if (raw.includes('*') && t === 'text') return lit('wild', raw, { s: raw })
    return lit('word', raw, { s: raw })
  }
  if (t === 'number') {
    const m = NUMBER_RE.exec(raw)
    if (!m) throw new QueryError(position, `Expected a number for ${spec.name}, got "${raw}"`)
    let n = parseFloat(m[1])
    const suffix = (m[2] || '').toLowerCase()
    if (suffix === 'k') n *= 1000
    else if (suffix === 'm') n *= 1_000_000
    return lit('number', raw, { n })
  }
  if (t === 'duration') {
    const m = DURATION_RE.exec(raw)
    if (!m) throw new QueryError(position, `Expected a duration for ${spec.name}, got "${raw}"`)
    const n = parseFloat(m[1])
    const unit = m[2] || 'ms'
    const ms = n * ({ ms: 1, s: 1000, m: 60_000, h: 3_600_000 } as Record<string, number>)[unit]
    return lit('duration', raw, { ms })
  }
  if (t === 'money') {
    const m = MONEY_RE.exec(raw)
    if (!m) throw new QueryError(position, `Expected an amount in USD for ${spec.name}, got "${raw}"`)
    return lit('money', raw, { usd: parseFloat(m[1]) })
  }
  if (t === 'date') {
    const low = raw.toLowerCase()
    if (DAY_RE.test(raw)) return lit('date', raw, { s: raw, gran: 'day' })
    if (MONTH_RE.test(raw)) return lit('date', raw, { s: raw, gran: 'month' })
    if (low === 'today' || low === 'yesterday') return lit('named', raw, { s: low })
    const m = REL_RE.exec(low)
    if (m) return lit('reldate', raw, { n: -parseInt(m[1], 10), unit: m[2] })
    throw new QueryError(position, `Expected a date for ${spec.name}, got "${raw}"`)
  }
  if (t === 'boolean') {
    const low = raw.toLowerCase()
    if (['true', 'yes', '1'].includes(low)) return lit('bool', raw, { b: true })
    if (['false', 'no', '0'].includes(low)) return lit('bool', raw, { b: false })
    throw new QueryError(position, `Expected true or false for ${spec.name}, got "${raw}"`)
  }
  throw new QueryError(position, `Unknown field type for ${spec.name}`)
}

// ---------------------------------------------------------------------------
// Parser (mirrors grammar._Parser method for method)
// ---------------------------------------------------------------------------

class Parser {
  s: string
  n: number
  i = 0
  tokens: Token[] = []
  terms = 0
  depth = 0

  constructor(text: string) {
    this.s = text
    this.n = text.length
  }

  ws() { while (this.i < this.n && isSpace(this.s[this.i])) this.i++ }
  peek(k = 0): string { const j = this.i + k; return j < this.n ? this.s[j] : '' }
  atEnd(): boolean { this.ws(); return this.i >= this.n }

  keywordAt(kw: string): boolean {
    this.ws()
    const end = this.i + kw.length
    if (this.s.slice(this.i, end).toUpperCase() !== kw) return false
    const nxt = end < this.n ? this.s[end] : ''
    return nxt === '' || isSpace(nxt) || nxt === '(' || nxt === ')'
  }

  readWord(): string {
    const start = this.i
    while (this.i < this.n && !isSpace(this.s[this.i]) && !'()"'.includes(this.s[this.i])) this.i++
    return this.s.slice(start, this.i)
  }

  readPhrase(): string {
    const start = this.i
    this.i++
    while (this.i < this.n && this.s[this.i] !== '"') this.i++
    if (this.i >= this.n) throw new QueryError(start, 'Unterminated quote')
    this.i++
    return this.s.slice(start + 1, this.i - 1)
  }

  parse(): Node | null {
    if (this.atEnd()) return null
    const node = this.parseOr()
    this.ws()
    if (this.i < this.n) {
      if (this.s[this.i] === ')') throw new QueryError(this.i, 'Unexpected ")"')
      throw new QueryError(this.i, 'Unexpected input')
    }
    return node
  }

  parseOr(): Node {
    const first = this.parseAnd()
    const children: Node[] = [first]
    for (;;) {
      if (this.keywordAt('OR')) {
        const kwPos = this.i
        this.tokens.push({ type: 'keyword', start: this.i, end: this.i + 2 })
        this.i += 2
        if (this.atEnd() || this.peek() === ')') throw new QueryError(kwPos, 'Expected a term after "OR"')
        children.push(this.parseAnd())
      } else break
    }
    if (children.length === 1) return first
    return { t: 'or', c: children, _pos: children[0]._pos, _end: children[children.length - 1]._end }
  }

  parseAnd(): Node {
    const children: Node[] = [this.parseNot()]
    for (;;) {
      this.ws()
      if (this.i >= this.n || this.peek() === ')' || this.keywordAt('OR')) break
      if (this.keywordAt('AND')) {
        const kwPos = this.i
        this.tokens.push({ type: 'keyword', start: this.i, end: this.i + 3 })
        this.i += 3
        if (this.atEnd() || this.peek() === ')') throw new QueryError(kwPos, 'Expected a term after "AND"')
      }
      children.push(this.parseNot())
    }
    if (children.length === 1) return children[0]
    return { t: 'and', c: children, _pos: children[0]._pos, _end: children[children.length - 1]._end }
  }

  parseNot(): Node {
    this.ws()
    const start = this.i
    if (this.keywordAt('NOT')) {
      this.tokens.push({ type: 'keyword', start: this.i, end: this.i + 3 })
      this.i += 3
      if (this.atEnd() || this.peek() === ')') throw new QueryError(start, 'Expected a term after "NOT"')
      const child = this.parseNot()
      return { t: 'not', c: child, _pos: start, _end: child._end }
    }
    if (this.peek() === '-' && this.peek(1) !== '' && !isSpace(this.peek(1))) {
      this.tokens.push({ type: 'keyword', start: this.i, end: this.i + 1 })
      this.i += 1
      const child = this.parseNot()
      return { t: 'not', c: child, _pos: start, _end: child._end }
    }
    return this.parsePrimary()
  }

  parsePrimary(): Node {
    this.ws()
    const start = this.i
    const ch = this.peek()
    if (ch === '(') {
      this.depth++
      if (this.depth > MAX_DEPTH) throw new QueryError(start, `Too much nesting (max ${MAX_DEPTH} levels)`)
      this.tokens.push({ type: 'paren', start: this.i, end: this.i + 1 })
      this.i++
      if (this.atEnd()) throw new QueryError(start, 'Expected ")"')
      if (this.peek() === ')') throw new QueryError(start, 'Expected a term inside "( )"')
      const inner = this.parseOr()
      this.ws()
      if (this.peek() !== ')') throw new QueryError(start, 'Expected ")"')
      this.tokens.push({ type: 'paren', start: this.i, end: this.i + 1 })
      this.i++
      this.depth--
      return { ...inner, _pos: start, _end: this.i } as Node
    }
    if (ch === ')') throw new QueryError(start, 'Unexpected ")"')
    for (const kw of ['AND', 'OR']) {
      if (this.keywordAt(kw)) throw new QueryError(start, `Expected a term before "${kw}"`)
    }
    return this.parseTerm()
  }

  countTerm(pos: number) {
    this.terms++
    if (this.terms > MAX_TERMS) throw new QueryError(pos, `Too many terms (max ${MAX_TERMS})`)
  }

  parseTerm(): Node {
    const start = this.i
    if (this.peek() === '"') {
      const s = this.readPhrase()
      this.tokens.push({ type: 'phrase', start, end: this.i })
      this.countTerm(start)
      return { t: 'text', phrase: true, s, _pos: start, _end: this.i }
    }
    FIELD_RE.lastIndex = this.i
    const m = FIELD_RE.exec(this.s)
    if (m && m.index === this.i) {
      const end = this.i + m[0].length
      if (end < this.n && this.s[end] === ':') {
        const name = m[0]
        this.i = end + 1
        return this.parseFieldTerm(name, start, end)
      }
    }
    const word = this.readWord()
    if (word === '') throw new QueryError(start, 'Unexpected input')
    this.tokens.push({ type: 'text', start, end: this.i })
    this.countTerm(start)
    return { t: 'text', phrase: false, s: word, _pos: start, _end: this.i }
  }

  parseFieldTerm(name: string, start: number, colonPos: number): Node {
    this.tokens.push({ type: 'field', start, end: colonPos })
    this.tokens.push({ type: 'colon', start: colonPos, end: colonPos + 1 })
    this.countTerm(start)

    if (name.toLowerCase() === 'has') {
      const targetStart = this.i
      const target = this.readWord()
      const spec = target ? resolveField(target) : undefined
      if (!spec) throw new QueryError(targetStart, `Unknown field "${target}" after "has:"`, target ? suggestFields(target) : [])
      this.tokens.push({ type: 'value', start: targetStart, end: this.i })
      return { t: 'term', field: spec.name, op: 'has', values: [], _pos: start, _end: this.i }
    }

    const spec = resolveField(name)
    if (!spec) throw new QueryError(start, `Unknown field "${name}"`, suggestFields(name))

    if (this.peek() === '(') {
      this.tokens.push({ type: 'paren', start: this.i, end: this.i + 1 })
      this.i++
      const values: Literal[] = []
      for (;;) {
        this.ws()
        if (this.peek() === ')') {
          if (values.length === 0) throw new QueryError(this.i, `Expected a value after "${spec.name}:"`)
          break
        }
        if (this.i >= this.n) throw new QueryError(this.i, 'Expected ")"')
        if (values.length) {
          if (!this.keywordAt('OR')) throw new QueryError(this.i, 'Expected "OR" or ")"')
          this.tokens.push({ type: 'keyword', start: this.i, end: this.i + 2 })
          this.i += 2
          this.ws()
        }
        values.push(this.parseScalarToken(spec))
      }
      this.tokens.push({ type: 'paren', start: this.i, end: this.i + 1 })
      this.i++
      if (!fieldOps(spec).includes('any')) throw new QueryError(start, `${spec.name} does not support "( OR )"`)
      return { t: 'term', field: spec.name, op: 'any', values, _pos: start, _end: this.i }
    }

    for (const op of CMP_OPS) {
      if (this.s.startsWith(op, this.i)) {
        const opPos = this.i
        this.tokens.push({ type: 'op', start: this.i, end: this.i + op.length })
        this.i += op.length
        if (this.i >= this.n || isSpace(this.s[this.i]) || this.s[this.i] === ')') {
          throw new QueryError(opPos, `Expected a value after "${spec.name}:${op}"`)
        }
        if (!fieldOps(spec).includes(CMP_NAME[op])) throw new QueryError(opPos, `${spec.name} does not support "${op}"`)
        const l = this.parseScalarToken(spec)
        return { t: 'term', field: spec.name, op: CMP_NAME[op], values: [l], _pos: start, _end: this.i }
      }
    }

    if (this.i >= this.n || isSpace(this.s[this.i]) || this.s[this.i] === ')') {
      throw new QueryError(this.i, `Expected a value after "${spec.name}:"`)
    }

    if (this.peek() !== '"') {
      const save = this.i
      const word = this.readWord()
      if (word.includes('..') && !word.startsWith('..') && !word.endsWith('..')) {
        const idx = word.indexOf('..')
        const lo = word.slice(0, idx)
        const hi = word.slice(idx + 2)
        if (!fieldOps(spec).includes('range')) throw new QueryError(save, `${spec.name} does not support ".."`)
        const loLit = parseScalar(spec, lo, false, save)
        const hiLit = parseScalar(spec, hi, false, save + lo.length + 2)
        this.tokens.push({ type: 'value', start: save, end: this.i })
        return { t: 'term', field: spec.name, op: 'range', values: [loLit, hiLit], _pos: start, _end: this.i }
      }
      this.i = save
    }

    const l = this.parseScalarToken(spec)
    const op = l.kind === 'wild' ? 'wild' : 'eq'
    return { t: 'term', field: spec.name, op, values: [l], _pos: start, _end: this.i }
  }

  parseScalarToken(spec: FieldSpec): Literal {
    this.ws()
    const start = this.i
    if (this.peek() === '"') {
      const raw = this.readPhrase()
      this.tokens.push({ type: 'phrase', start, end: this.i })
      return parseScalar(spec, raw, true, start)
    }
    const raw = this.readWord()
    if (raw === '') throw new QueryError(start, `Expected a value after "${spec.name}:"`)
    const l = parseScalar(spec, raw, false, start)
    this.tokens.push({ type: 'value', start, end: this.i })
    return l
  }
}

export interface ParseResult { ast: Ast; tokens: Token[] }

/** Parse a query. Throws QueryError with a character position. */
export function parse(text: string): ParseResult {
  if (text.length > MAX_LENGTH) throw new QueryError(MAX_LENGTH, `Query is too long (max ${MAX_LENGTH} characters)`)
  const p = new Parser(text)
  const root = p.parse()
  p.tokens.sort((a, b) => a.start - b.start)
  return { ast: { v: AST_VERSION, root }, tokens: p.tokens }
}

/** parse() that never throws: returns the error instead. */
export function tryParse(text: string): { result?: ParseResult; error?: QueryError } {
  try {
    return { result: parse(text) }
  } catch (e) {
    if (e instanceof QueryError) return { error: e }
    throw e
  }
}

// ---------------------------------------------------------------------------
// Canonical form and helpers (mirror grammar.py)
// ---------------------------------------------------------------------------

export function stripPositions(node: any): any {
  if (Array.isArray(node)) return node.map(stripPositions)
  if (node && typeof node === 'object') {
    const out: any = {}
    for (const [k, v] of Object.entries(node)) if (!k.startsWith('_')) out[k] = stripPositions(v)
    return out
  }
  return node
}

function litText(l: Literal): string {
  if (l.kind === 'phrase') return '"' + l.s + '"'
  if (l.kind === 'word') return l.s as string
  return l.raw
}

function nodeText(node: Node, parent: string | null): string {
  switch (node.t) {
    case 'text':
      return node.phrase ? '"' + node.s + '"' : node.s
    case 'term': {
      const { field, op, values } = node
      if (op === 'has') return `has:${field}`
      if (op === 'any') return `${field}:(` + values.map(litText).join(' OR ') + ')'
      if (op === 'range') return `${field}:${values[0].raw}..${values[1].raw}`
      const prefix = ({ gt: '>', gte: '>=', lt: '<', lte: '<=' } as Record<string, string>)[op] || ''
      return `${field}:${prefix}${litText(values[0])}`
    }
    case 'not':
      return 'NOT ' + nodeText(node.c, 'not')
    case 'and':
    case 'or': {
      const joiner = node.t === 'and' ? ' AND ' : ' OR '
      const s = node.c.map(c => nodeText(c, node.t)).join(joiner)
      if (parent === 'not' || (parent === 'and' && node.t === 'or')) return '(' + s + ')'
      return s
    }
  }
}

export function canonical(ast: Ast): string {
  return ast.root ? nodeText(ast.root, null) : ''
}

/** Terms ANDed at the top level (drives quick-filter chip state). */
export function topLevelTerms(ast: Ast): Node[] {
  const root = ast.root
  if (!root) return []
  if (root.t === 'and') return root.c.filter(c => c.t === 'term' || c.t === 'text')
  if (root.t === 'term' || root.t === 'text') return [root]
  return []
}

export function mentionsField(ast: Ast, name: string): boolean {
  const walk = (node: Node | null): boolean => {
    if (!node) return false
    if (node.t === 'term') return node.field === name
    if (node.t === 'not') return walk(node.c)
    if (node.t === 'and' || node.t === 'or') return node.c.some(walk)
    return false
  }
  return walk(ast.root)
}

// ---------------------------------------------------------------------------
// Editing helpers — chips and the filter builder write terms into the query
// ---------------------------------------------------------------------------

function termKey(node: Node): string {
  return canonical({ v: AST_VERSION, root: node })
}

/** Is every top-level term of `chipQuery` present as a top-level term of `q`? */
export function containsTerms(q: string, chipQuery: string): boolean {
  const a = tryParse(q).result
  const b = tryParse(chipQuery).result
  if (!a || !b) return false
  const have = new Set(topLevelTerms(a.ast).map(termKey))
  const want = topLevelTerms(b.ast).map(termKey)
  return want.length > 0 && want.every(k => have.has(k))
}

/** Append `terms` (a query fragment) to `q` as top-level AND terms. */
export function addTerms(q: string, terms: string): string {
  const base = q.trim()
  const extra = terms.trim()
  if (!extra) return base
  if (!base) return extra
  const parsed = tryParse(base).result
  // A query whose root is an OR must be grouped before adding an AND term.
  if (parsed?.ast.root?.t === 'or') return `(${base}) ${extra}`
  return `${base} ${extra}`
}

/** Remove every top-level term of `terms` from `q`, preserving the rest verbatim. */
export function removeTerms(q: string, terms: string): string {
  const parsed = tryParse(q).result
  const chip = tryParse(terms).result
  if (!parsed || !chip) return q
  const drop = new Set(topLevelTerms(chip.ast).map(termKey))
  const spans = topLevelTerms(parsed.ast)
    .filter(n => drop.has(termKey(n)))
    .map(n => [n._pos as number, n._end as number] as [number, number])
    .sort((x, y) => y[0] - x[0])
  let out = q
  for (const [start, end] of spans) {
    // Also swallow a preceding explicit AND so "a AND b" minus b is "a".
    let s = start
    const before = out.slice(0, start)
    const m = /\s+AND\s*$/i.exec(before)
    if (m) s = m.index
    out = out.slice(0, s) + out.slice(end)
  }
  return out.replace(/\s+/g, ' ').trim()
}

// ---------------------------------------------------------------------------
// Cursor context — what to suggest while typing
// ---------------------------------------------------------------------------

export type CursorContext =
  | { kind: 'field'; prefix: string; start: number }
  | { kind: 'value'; field: FieldSpec; prefix: string; start: number; op: string }
  | { kind: 'none' }

/** What the caret is inside of: a field name being typed, a value for a known
 *  field, or nothing worth suggesting. Pure text analysis — works while the
 *  query does not parse yet. */
export function cursorContext(q: string, caret: number): CursorContext {
  const before = q.slice(0, caret)
  // Inside an open phrase → no suggestions
  if ((before.match(/"/g) || []).length % 2 === 1) return { kind: 'none' }
  const m = /(?:^|[\s(])(-?)([A-Za-z_][A-Za-z0-9_.]*)(?::(\(?)((?:>=|<=|>|<)?)([^\s()"]*))?$/.exec(before)
  if (!m) return { kind: 'none' }
  const name = m[2]
  const paren = m[3]
  const op = m[4]
  const value = m[5]
  const hasColon = m[0].includes(':')
  if (!hasColon) {
    return { kind: 'field', prefix: name, start: caret - name.length }
  }
  const spec = resolveField(name)
  if (!spec) return { kind: 'none' }
  const prefix = value || ''
  return { kind: 'value', field: spec, prefix, start: caret - prefix.length, op: op || (paren ? 'any' : 'eq') }
}
