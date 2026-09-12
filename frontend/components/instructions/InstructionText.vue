<template>
  <span v-if="!markdown" dir="auto" class="whitespace-pre-wrap text-[13px] leading-relaxed text-gray-900 dark:text-white">
    <template v-for="(segment, i) in segments" :key="i">
      <span
        v-if="segment.ref || segment.mention"
        class="inline-flex items-center gap-0.5 px-1 py-0.5 rounded bg-indigo-50 dark:bg-indigo-400/[0.18] border border-indigo-100 dark:border-transparent text-[11px] font-sans font-medium text-indigo-700 dark:text-indigo-200 align-baseline"
      >
        <template v-if="segment.ref">
          <DataSourceIcon
            v-if="segment.ref.data_source_type || segment.ref.data_source_icon"
            :type="segment.ref.data_source_type"
            :icon="segment.ref.data_source_icon"
            class="h-3 flex-shrink-0"
          />
          <Icon
            v-else-if="segment.ref.type === 'instruction'"
            name="heroicons:document-text"
            class="w-3 h-3 flex-shrink-0 text-indigo-400"
          />
          <Icon
            v-else
            name="heroicons:table-cells"
            class="w-3 h-3 flex-shrink-0 text-blue-400"
          />
          <Icon
            v-if="segment.ref.type === 'connection_tool'"
            name="heroicons:wrench-screwdriver"
            class="w-2.5 h-2.5 flex-shrink-0 text-indigo-300"
          />
          <span>@{{ segment.ref.name || segment.raw }}</span>
        </template>
        <span v-else>@{{ segment.mention }}</span>
      </span>
      <span v-else>{{ segment.text }}</span>
    </template>
  </span>
  <div v-else class="instruction-prose">
    <template v-for="(block, i) in blocks" :key="i">
      <DocMermaid v-if="block.type === 'mermaid'" :code="block.code || ''" />
      <div v-else v-html="block.html" />
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import DocMermaid from '~/components/dashboard/DocMermaid.vue'
import { firstStrongDir } from '~/utils/textDirection'
import {
  buildMentionMatcher,
  parseMentionSegments,
  replaceMentions,
  type MentionMatcher,
} from '~/utils/mentions'

interface RawReference {
  id?: string
  type?: string
  object_type?: string
  name?: string | null
  display_text?: string | null
  data_source_type?: string | null
}

interface Reference {
  id: string
  type: string
  name: string | null
  data_source_type: string | null
}

const props = defineProps<{
  text: string
  references?: RawReference[]
  prose?: boolean  // kept for compatibility, no longer affects font
  markdown?: boolean
}>()

const normalizedRefs = computed((): Reference[] =>
  (props.references || []).map(r => ({
    id: r.id || '',
    type: r.type || r.object_type || '',
    name: r.name || r.display_text || null,
    data_source_type: r.data_source_type || null,
  }))
)

const refByName = computed(() => {
  const map = new Map<string, Reference>()
  for (const ref of normalizedRefs.value) {
    const key = (ref.name || '').toLowerCase()
    if (key) map.set(key, ref)
  }
  return map
})

interface Segment {
  text?: string
  ref?: Reference
  mention?: string
  raw?: string
}

// The instruction's own references double as the mention dictionary: their
// display names are exactly the labels that may appear after an '@'. Indexed as
// a trie so lookup cost tracks the matched name's length, not the number of
// references (see utils/mentions.ts).
const mentionMatcher = computed(() => buildMentionMatcher(normalizedRefs.value))

const segments = computed((): Segment[] =>
  parseMentionSegments(props.text || '', mentionMatcher.value).map((seg): Segment => {
    if (seg.type === 'text') return { text: seg.value }
    const ref = refByName.value.get(seg.label.toLowerCase())
    return ref ? { ref, raw: seg.label } : { mention: seg.label }
  })
)

// ─── Markdown rendering ──────────────────────────────────────────────────────
// Mirrors InstructionEditor's pipeline so read-only and edit views render identically.

const md = new MarkdownIt({ html: true, breaks: false, linkify: false })

// Per-block direction, mirroring the editor's auto-dir decorations: each block
// token gets an explicit dir from its first strong character, so RTL blocks
// right-align and list markers / blockquote bars flip to the correct edge
// (via the logical CSS below). Code blocks are skipped — they stay LTR.
const DIR_OPEN_TOKENS = new Set([
  'paragraph_open', 'heading_open', 'bullet_list_open', 'ordered_list_open', 'list_item_open', 'blockquote_open',
  // Tables need it most: without a dir of their own an RTL table inherits the
  // document's LTR and lays its columns out left-to-right, mirroring the order
  // they were authored in. Cells carry their own dir as well, so a table mixing
  // Hebrew labels with English identifiers aligns each cell by its own content.
  'table_open', 'tr_open', 'th_open', 'td_open',
])

// A table wider than the panel has nowhere to go: it is squeezed until every
// cell wraps. Wrap each one in its own scroll container (the editor gets the
// equivalent for free from the Table extension's `.tableWrapper`).
md.renderer.rules.table_open = (tokens, idx, options, _env, self) =>
  '<div class="md-table-wrap">' + self.renderToken(tokens, idx, options)
md.renderer.rules.table_close = (tokens, idx, options, _env, self) =>
  self.renderToken(tokens, idx, options) + '</div>'

md.core.ruler.push('block_dir', (state) => {
  const tokens = state.tokens
  for (let i = 0; i < tokens.length; i++) {
    const open = tokens[i]
    if (!DIR_OPEN_TOKENS.has(open.type)) continue
    let dir: 'rtl' | 'ltr' | null = null
    for (let j = i + 1; j < tokens.length && !dir; j++) {
      const t = tokens[j]
      if (t.level <= open.level) break // reached the matching close token
      if (t.type !== 'inline') continue
      for (const child of t.children || []) {
        if (child.type !== 'text' && child.type !== 'code_inline') continue
        dir = firstStrongDir(child.content)
        if (dir) break
      }
    }
    if (dir) open.attrSet('dir', dir)
  }
})

// Same dictionary-driven parse as the plain-text branch above, so a multi-word
// mention chips identically whether the instruction renders as markdown or not.
function preprocessMentions(text: string, matcher: MentionMatcher | null): string {
  return replaceMentions(text, matcher, (label) => {
    const safe = label.replace(/&/g, '&amp;').replace(/"/g, '&quot;')
    return `<span class="instruction-mention">@${safe}</span>`
  })
}

// Instruction text is not only hand-written: instructions are drafted by the
// agent and learned from sessions, and the renderer runs with html:true so
// authored HTML passes through. Straight into v-html that is a script-execution
// path, so sanitize exactly as DocViewer does before rendering markdown.
function renderProse(text: string, matcher: MentionMatcher | null): string {
  if (!text.trim()) return ''
  return DOMPurify.sanitize(md.render(preprocessMentions(text, matcher)))
}

// Split the markdown into prose blocks and ```mermaid diagram blocks, so a
// flowchart authored in an instruction renders as a diagram (via DocMermaid,
// which also repairs the common unquoted-label mistake) instead of a raw code
// block. Other fenced code (```sql, ```python, …) stays inline in the prose.
interface Block { type: 'html' | 'mermaid'; html?: string; code?: string }

const FENCE_RE = /^\s*(```|~~~)\s*(\S*)\s*$/

const blocks = computed<Block[]>(() => {
  const lines = (props.text || '').split('\n')
  const out: Block[] = []
  let buffer: string[] = []
  const matcher = mentionMatcher.value
  const flush = () => {
    const html = renderProse(buffer.join('\n'), matcher)
    if (html.trim()) out.push({ type: 'html', html })
    buffer = []
  }

  let i = 0
  while (i < lines.length) {
    const fence = lines[i].match(FENCE_RE)
    if (fence) {
      const marker = fence[1]
      const lang = (fence[2] || '').toLowerCase()
      if (lang === 'mermaid') {
        i++
        const body: string[] = []
        while (i < lines.length && !lines[i].trim().startsWith(marker)) { body.push(lines[i]); i++ }
        if (i < lines.length) i++ // consume closing fence
        flush()
        out.push({ type: 'mermaid', code: body.join('\n') })
      } else {
        // Non-mermaid fence: keep it verbatim in the prose buffer.
        buffer.push(lines[i]); i++
        while (i < lines.length && !lines[i].trim().startsWith(marker)) { buffer.push(lines[i]); i++ }
        if (i < lines.length) { buffer.push(lines[i]); i++ }
      }
      continue
    }
    buffer.push(lines[i]); i++
  }
  flush()
  return out
})
</script>

<style scoped>
/* Element typography for the rendered markdown lives in
   assets/css/instruction-prose.css, shared with InstructionEditor's
   `.tiptap-prose` so the read-only view and the editor cannot drift apart.
   Only the mention chip — whose class differs between the two — stays here. */

.instruction-prose :deep(.instruction-mention) {
  background-color: rgba(99, 102, 241, 0.12);
  color: #4338ca;
  border-radius: 4px;
  padding: 1px 4px;
  font-weight: 500;
  font-size: 0.95em;
  white-space: nowrap;
}

/* The `.dark` class lives on <html> (Tailwind darkMode: 'class'), outside this
   component's scope, so this is authored as :global and matched by the unique
   `.instruction-prose` class. */
:global(.dark .instruction-prose .instruction-mention) {
  background-color: rgba(129, 140, 248, 0.18);
  color: #c7d2fe;
}
</style>
