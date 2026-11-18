/**
 * Citations in an answer: `{{cite:<visualization id>}}`.
 *
 * The planner marks a finding with the result it came from (grammar and
 * server-side handling: backend/app/ai/citations.py). Here the marker becomes
 * a numbered source chip: it is rewritten to a markdown link
 * `[n](#cite-<source key>)`, which the transcript styles as a chip and wires
 * for hover/focus (preview) and click (open the result).
 *
 * Pure functions — no Vue reactivity — unit-tested in
 * tests/unit/useCitations.mjs.
 *
 * Rules:
 * - A source is a successful `create_data` result. Its key is the id of the
 *   block that produced it.
 * - A marker names a source by visualization id, in full or by its leading
 *   characters (8 or more). A marker that names no source — or more than one —
 *   is removed, never shown: a made-up citation must not read as a source.
 * - Sources are numbered per message, in order of first mention.
 * - Text inside a code fence is quoted, not cited.
 */
import type { DataOutput } from './useRunActivity'

export const CITATION_HREF_PREFIX = '#cite-'
const MIN_ID_CHARS = 8
const MARKER_RE = /[ \t]*\{\{\s*cite\s*:\s*([0-9A-Za-z-]{1,64})\s*\}\}/g
// Older answers sometimes put citations in their own paragraph. Keep those
// markers with the preceding text, without rewriting the stored answer.
const STANDALONE_CITATIONS_RE = /(\S)[ \t]*(?:\r?\n[ \t]*)+(\{\{[ \t]*cite[ \t]*:[ \t]*[0-9A-Za-z-]{1,64}[ \t]*\}\}(?:[ \t]*\{\{[ \t]*cite[ \t]*:[ \t]*[0-9A-Za-z-]{1,64}[ \t]*\}\})*)[ \t]*(?=\r?\n|$)/g
const CODE_FENCE_RE = /(```[\s\S]*?```)/
// The start of a marker still being streamed: "{", "{{", "{{ci", "{{cite:3f9a",
// "{{cite:3f9a1c2e}" …
const PARTIAL_MARKER_RE = /[ \t]*\{(?:\{(?:\s*(?:c(?:i(?:t(?:e(?:\s*(?::[^{}]*\}?)?)?)?)?)?)?)?)?$/

export interface CitationSource {
  /** Id of the block whose create_data call produced the result. */
  key: string
  /** Visualization ids of the result. */
  ids: string[]
  output: DataOutput
  toolExecution: any
}

export interface CitedText {
  /** The text with markers rewritten to chip links (or removed). */
  markdown: string
  /** Sources this text cites, in order of first mention. */
  cited: CitationSource[]
}

const compact = (v: string) => String(v || '').toLowerCase().replace(/[^0-9a-z]/g, '')

function vizIds(te: any): string[] {
  const out: string[] = []
  const push = (v: any) => { const s = String(v || ''); if (s && !out.includes(s)) out.push(s) }
  for (const id of te?.result_json?.created_visualization_ids || []) push(id)
  for (const viz of te?.created_visualizations || []) push(viz?.id)
  return out
}

/**
 * Every result in `blocks` that an answer can cite, in order.
 *
 * `describe` is `describeDataOutput` from useRunActivity, passed in so this
 * module stays dependency-free for the bundler-less unit tests.
 */
export function collectSources(blocks: any[], describe: (te: any) => DataOutput): CitationSource[] {
  const out: CitationSource[] = []
  for (const b of blocks || []) {
    const te = b?.tool_execution
    if (te?.tool_name !== 'create_data' || te.status !== 'success') continue
    const ids = vizIds(te)
    if (!ids.length) continue
    const output = describe(te)
    if (output.kind === 'none') continue
    out.push({ key: String(b.id), ids, output, toolExecution: te })
  }
  return out
}

/** The one source `raw` names, or null when it names none or several. */
export function matchSource(raw: string, sources: CitationSource[]): CitationSource | null {
  const wanted = compact(raw)
  if (wanted.length < MIN_ID_CHARS) return null
  const hits = sources.filter((s) => s.ids.some((id) => compact(id).startsWith(wanted)))
  return hits.length === 1 ? hits[0] : null
}

function eachProse(text: string, convert: (prose: string, last: boolean) => string): string {
  const parts = text.split(CODE_FENCE_RE)
  return parts.map((p, i) => (i % 2 ? p : convert(p, i === parts.length - 1))).join('')
}

/** `text` without any citation marker (clipboard, plain-text uses). */
export function stripCitations(text: string): string {
  if (!text || !text.includes('{{')) return text || ''
  return eachProse(text, (prose) => prose.replace(MARKER_RE, ''))
}

/**
 * Rewrite the markers in `text` to numbered chip links.
 *
 * `numbering` carries the message's numbering across its blocks: pass the same
 * array for every block of one message, in order. `streaming` hides the start
 * of a marker that has not finished arriving, so the reader never sees
 * "{{cite:3f" flash by.
 */
export function citeText(
  text: string,
  sources: CitationSource[],
  numbering: CitationSource[] = [],
  opts?: { streaming?: boolean },
): CitedText {
  const cited: CitationSource[] = []
  if (!text || !text.includes('{')) return { markdown: text || '', cited }
  const markdown = eachProse(text, (prose, last) => {
    let out = prose.replace(STANDALONE_CITATIONS_RE, '$1 $2').replace(MARKER_RE, (_m, raw: string) => {
      const source = matchSource(raw, sources)
      if (!source) return ''
      if (!numbering.includes(source)) numbering.push(source)
      if (!cited.includes(source)) cited.push(source)
      // No-break space: the chip stays on the line of the sentence it follows.
      // The link title carries the source's name for the chip's accessible label.
      const title = source.output.title.replace(/["\\\n]/g, ' ').trim()
      const link = `${CITATION_HREF_PREFIX}${source.key}${title ? ` "${title}"` : ''}`
      return `\u00A0[${numbering.indexOf(source) + 1}](${link})`
    })
    if (opts?.streaming && last) out = out.replace(PARTIAL_MARKER_RE, '')
    return out
  })
  return { markdown, cited }
}

/** The source key a chip link points at, or '' for any other href. */
export function citationKeyFromHref(href: string | null | undefined): string {
  // Only a chip's own href, which starts with the prefix (as ChatLink checks):
  // an ordinary link that merely contains `#cite-` must keep navigating.
  const h = String(href || '')
  if (!h.startsWith(CITATION_HREF_PREFIX)) return ''
  const raw = h.slice(CITATION_HREF_PREFIX.length)
  try { return decodeURIComponent(raw) } catch { return raw }
}
