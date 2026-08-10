/**
 * @mention parsing.
 *
 * A mention is `@` followed by the *display name* of a referenced object. Display
 * names are not identifiers: tables, semantic models and MCP tools routinely carry
 * spaces, slashes and dots — "Sales Orders", "Microsoft Fabric / dbo.sales".
 *
 * A regex therefore cannot delimit a mention on its own. Given the text
 * `@Sales Orders by region`, nothing in the string says whether the name is
 * "Sales", "Sales Orders" or "Sales Orders by region" — that depends entirely on
 * which names exist. Matching `[A-Za-z0-9_]+` just takes the first word and
 * silently truncates every multi-word name.
 *
 * So mentions are parsed against a dictionary of known names, longest match wins
 * (maximum munch — the same rule lexers use for `>>` vs `>`). The dictionary lives
 * in a trie, so a lookup costs O(length of the matched name) instead of
 * O(number of names): an agent with 5k tables parses at the same speed as one with
 * five. Without a dictionary the parser degrades to the historical
 * identifier-shaped match, so callers that have no name list still work.
 *
 * Quoted mentions (`@"Sales Orders"`) are always honored, dictionary or not —
 * that is the form the mention picker inserts, and it is self-delimiting.
 */

/** Identifier-shaped fallback: what a mention looks like with no dictionary. */
const BARE_MENTION_RE = /^[A-Za-z_][A-Za-z0-9_]*(?:[.\-][A-Za-z0-9_]+)*/

/** A name needs quoting when its own characters would not survive a bare parse. */
const NEEDS_QUOTES_RE = /[\s\-."]/

/** Characters that may not immediately follow a dictionary match — a match must
 *  end on a word boundary, so "Sales" does not chip the head of "@SalesForce". */
const WORD_CHAR_RE = /[A-Za-z0-9_]/

interface TrieNode {
  children: Map<string, TrieNode>
  /** Canonical display name, set on terminal nodes. */
  name?: string
}

export interface MentionMatch {
  /** Canonical name as it appears in the dictionary (original casing). */
  name: string
  /** Characters consumed from the text (may differ in case from `name`). */
  length: number
}

/**
 * A dictionary of mentionable display names, indexed for longest-prefix lookup.
 *
 * Matching is case-insensitive; the canonical casing from the dictionary is
 * returned so a chip renders the object's real name regardless of how it was
 * typed.
 */
export class MentionMatcher {
  private root: TrieNode = { children: new Map() }
  readonly size: number

  constructor(names: Iterable<string | null | undefined>) {
    let size = 0
    for (const raw of names) {
      const name = (raw || '').trim()
      if (!name) continue
      let node = this.root
      const lower = name.toLowerCase()
      for (const ch of lower) {
        let next = node.children.get(ch)
        if (!next) {
          next = { children: new Map() }
          node.children.set(ch, next)
        }
        node = next
      }
      // First writer wins, so a stable dictionary order gives stable casing.
      if (node.name === undefined) {
        node.name = name
        size++
      }
    }
    this.size = size
  }

  get isEmpty(): boolean {
    return this.size === 0
  }

  /**
   * Longest dictionary name matching `text` at `pos`, or null.
   *
   * Walks the trie one character at a time, remembering the last terminal node
   * seen — so the longest candidate wins and the scan stops as soon as no name
   * can extend further. Cost is bounded by the longest name in the dictionary,
   * not by its size.
   */
  matchAt(text: string, pos: number): MentionMatch | null {
    let node = this.root
    let best: MentionMatch | null = null
    for (let i = pos; i < text.length; i++) {
      const next = node.children.get(text[i].toLowerCase())
      if (!next) break
      node = next
      if (node.name !== undefined) {
        const after = text[i + 1]
        // Require a word boundary so a short name cannot chip the head of a
        // longer bare token ("Sales" inside "@SalesForce").
        if (after === undefined || !WORD_CHAR_RE.test(after)) {
          best = { name: node.name, length: i + 1 - pos }
        }
      }
    }
    return best
  }
}

/** Empty dictionary — parses with the identifier fallback only. */
export const EMPTY_MENTION_MATCHER = new MentionMatcher([])

/**
 * Build a matcher from whatever reference-ish objects a caller has on hand.
 * Accepts the shapes returned by `/instructions/available-references` and by an
 * instruction's own `references` array.
 */
export function buildMentionMatcher(
  refs: Array<{ name?: string | null; display_text?: string | null } | null | undefined> | null | undefined,
): MentionMatcher {
  if (!refs?.length) return EMPTY_MENTION_MATCHER
  return new MentionMatcher(refs.map(r => r?.name || r?.display_text || null))
}

export type MentionSegment =
  | { type: 'text'; value: string }
  | { type: 'mention'; label: string; /** consumed source text, without the `@` */ raw: string }

/**
 * Split `text` into literal runs and mentions.
 *
 * Resolution order at each `@`:
 *   1. a quoted label — `@"Sales Orders"` — always wins, it is self-delimiting;
 *   2. the longest name in `matcher`;
 *   3. the identifier-shaped fallback, so undictionaried text still chips.
 */
export function parseMentionSegments(
  text: string,
  matcher: MentionMatcher | null = null,
): MentionSegment[] {
  const src = text || ''
  const out: MentionSegment[] = []
  let buffer = ''
  const flush = () => {
    if (buffer) {
      out.push({ type: 'text', value: buffer })
      buffer = ''
    }
  }

  let i = 0
  while (i < src.length) {
    if (src[i] === '@') {
      const rest = src.slice(i + 1)

      // 1. Quoted mention.
      if (rest[0] === '"') {
        const end = rest.indexOf('"', 1)
        if (end !== -1) {
          const label = rest.slice(1, end)
          flush()
          out.push({ type: 'mention', label, raw: `"${label}"` })
          i += 1 + end + 1
          continue
        }
      }

      // 2. Longest known display name.
      const match = matcher?.matchAt(src, i + 1)
      if (match) {
        flush()
        out.push({ type: 'mention', label: match.name, raw: src.substr(i + 1, match.length) })
        i += 1 + match.length
        continue
      }

      // 3. Identifier-shaped fallback.
      const word = rest.match(BARE_MENTION_RE)
      if (word) {
        flush()
        out.push({ type: 'mention', label: word[0], raw: word[0] })
        i += 1 + word[0].length
        continue
      }
    }

    buffer += src[i]
    i++
  }

  flush()
  return out
}

/** Distinct mention labels in `text`, in order of first appearance. */
export function extractMentionLabels(
  text: string,
  matcher: MentionMatcher | null = null,
): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const seg of parseMentionSegments(text, matcher)) {
    if (seg.type !== 'mention') continue
    const key = seg.label.toLowerCase()
    if (seen.has(key)) continue
    seen.add(key)
    out.push(seg.label)
  }
  return out
}

/**
 * Rewrite every mention in `text` through `render`, leaving literal runs alone.
 * Used to turn stored markdown into chip markup.
 */
export function replaceMentions(
  text: string,
  matcher: MentionMatcher | null,
  render: (label: string) => string,
): string {
  return parseMentionSegments(text, matcher)
    .map(seg => (seg.type === 'mention' ? render(seg.label) : seg.value))
    .join('')
}

/** True when `name` must be written as `@"name"` to survive a bare parse. */
export function mentionNeedsQuotes(name: string | null | undefined): boolean {
  return !!name && NEEDS_QUOTES_RE.test(name)
}

/** Canonical source form for a mention of `name`. */
export function formatMention(name: string): string {
  return mentionNeedsQuotes(name) ? `@"${name.replace(/"/g, '')}"` : `@${name}`
}

// ---------------------------------------------------------------------------
// Prompt mentions -> InstructionText references
// ---------------------------------------------------------------------------

/** Prompt mention groups are keyed by display label; references by object type. */
const GROUP_TYPE_MAP: Record<string, string> = {
  'DATA SOURCES': 'data_source',
  'TABLES': 'datasource_table',
  'FILES': 'file',
  'ENTITIES': 'entity',
  'CONNECTION TOOLS': 'connection_tool',
}

export interface PromptMentionRef {
  id: string
  type: string
  name: string
  data_source_type?: string
}

/**
 * Flatten a completion prompt's `mentions` groups into the reference list
 * InstructionText matches against, so an @mention in a user prompt renders as a
 * chip rather than bare text. Shared by the report view and the public share
 * view — the two render the same prompts and must resolve them identically.
 */
export function promptMentionsToRefs(
  mentions?: Array<{ name: string; items: any[] }>,
): PromptMentionRef[] {
  if (!mentions?.length) return []
  const refs: PromptMentionRef[] = []
  for (const group of mentions) {
    const type = GROUP_TYPE_MAP[(group.name || '').toUpperCase()] || 'entity'
    for (const item of group.items || []) {
      let name = item.name || item.title || item.filename || ''
      // Data-source tables are serialized into the prompt text with their
      // source prefix (e.g. "@Microsoft Fabric / dbo.sales"), so the ref
      // name must include it to match and render as a single mention chip.
      if (type === 'datasource_table') {
        const prefix = item.connection_name || item.data_source_name
        if (prefix) name = `${prefix} / ${name}`
      }
      refs.push({
        id: item.id,
        type,
        // `icon_type` is what the mention items actually carry (set to the
        // data source's type); include it so the chip renders the correct
        // data-source icon instead of the generic fallback glyph.
        data_source_type: item.connection_type || item.data_source_type || item.icon_type || undefined,
        name,
      })
    }
  }
  return refs
}
