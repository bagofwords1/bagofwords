<template>
  <div class="instruction-wysiwyg" ref="containerRef" :dir="containerDir">
    <!-- WYSIWYG mode (v-show keeps EditorContent in DOM so ProseMirror stays attached) -->
    <div v-show="mode === 'wysiwyg' && !editorFailed">
      <!-- Floating bubble toolbar (appears on text selection, edit mode only) -->
      <BubbleMenu
        v-if="editor && isEditable"
        :editor="editor"
        :tippy-options="{ duration: 100, placement: 'top', maxWidth: '400px' }"
      >
        <div class="bubble-toolbar">
          <button type="button" class="bubble-btn" :class="{ active: editor.isActive('bold') }" @click="editor.chain().focus().toggleBold().run()">
            <strong>B</strong>
          </button>
          <button type="button" class="bubble-btn" :class="{ active: editor.isActive('italic') }" @click="editor.chain().focus().toggleItalic().run()">
            <em>I</em>
          </button>
          <button type="button" class="bubble-btn" :class="{ active: editor.isActive('strike') }" @click="editor.chain().focus().toggleStrike().run()">
            <s>S</s>
          </button>
          <div class="bubble-sep" />
          <button type="button" class="bubble-btn text-xs font-medium" :class="{ active: editor.isActive('heading', { level: 1 }) }" @click="editor.chain().focus().toggleHeading({ level: 1 }).run()">H1</button>
          <button type="button" class="bubble-btn text-xs font-medium" :class="{ active: editor.isActive('heading', { level: 2 }) }" @click="editor.chain().focus().toggleHeading({ level: 2 }).run()">H2</button>
          <div class="bubble-sep" />
          <button type="button" class="bubble-btn" :class="{ active: editor.isActive('bulletList') }" @click="editor.chain().focus().toggleBulletList().run()" title="Bullet list">
            <Icon name="heroicons:list-bullet" class="w-3.5 h-3.5" />
          </button>
          <button type="button" class="bubble-btn" :class="{ active: editor.isActive('code') }" @click="editor.chain().focus().toggleCode().run()" title="Inline code">
            <Icon name="heroicons:code-bracket" class="w-3.5 h-3.5" />
          </button>
        </div>
      </BubbleMenu>

      <!-- Tiptap content area -->
      <div class="relative">
        <EditorContent :editor="editor" class="wysiwyg-content" />
        <!-- Placeholder when empty -->
        <div
          v-if="editor?.isEmpty && isEditable"
          class="absolute top-2 start-0 text-xs text-gray-400 dark:text-gray-600 pointer-events-none select-none whitespace-pre-line"
        >{{ placeholder || 'Write instructions using markdown... (type @ to mention a table or instruction)' }}</div>
      </div>

      <!-- @mention dropdown -->
      <div
        v-if="mentionState.active && isEditable"
        ref="dropdownRef"
        class="absolute z-50 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg max-h-48 overflow-y-auto w-80"
        :style="{ top: mentionState.position.top + 'px', left: mentionState.position.left + 'px' }"
      >
        <div v-if="mentionState.items.length === 0" class="px-3 py-2 text-xs text-gray-500 dark:text-gray-400">
          {{ mentionState.query.length < 1 ? 'Type to search...' : 'No results' }}
        </div>
        <button
          v-for="(item, i) in mentionState.items"
          :key="item.id"
          type="button"
          :data-idx="i"
          class="w-full text-start px-3 py-2 text-xs hover:bg-gray-50 dark:hover:bg-gray-800 flex items-start gap-2 border-b border-gray-100 dark:border-gray-800 last:border-0"
          :class="{ 'bg-blue-50 dark:bg-blue-950': i === mentionState.selectedIndex }"
          @mousedown.prevent="selectMentionItem(item)"
        >
          <Icon
            :name="item.type === 'instruction' ? 'heroicons:cube' : item.type === 'connection_tool' ? 'heroicons:wrench-screwdriver' : 'heroicons:table-cells'"
            class="w-3.5 h-3.5 mt-0.5 shrink-0"
            :class="item.type === 'instruction' ? 'text-indigo-500' : item.type === 'connection_tool' ? 'text-gray-500 dark:text-gray-400' : 'text-blue-500'"
          />
          <div class="flex-1 min-w-0">
            <template v-if="item.type === 'instruction'">
              <span v-if="item.name" class="font-mono font-medium text-gray-900 dark:text-white block">{{ item.name }}</span>
              <span v-else class="text-gray-700 dark:text-gray-300 truncate block">"{{ item.textPreview?.slice(0, 30) }}..."</span>
              <span v-if="item.name && item.textPreview" class="text-[10px] text-gray-500 dark:text-gray-400 truncate block">{{ item.textPreview }}</span>
            </template>
            <template v-else-if="item.type === 'connection_tool'">
              <span class="font-mono font-medium text-gray-900 dark:text-white block">{{ item.name }}</span>
              <span v-if="item.textPreview" class="text-[10px] text-gray-500 dark:text-gray-400 truncate block">{{ item.textPreview }}</span>
              <span v-if="item.dataSourceName" class="text-[10px] text-gray-400 dark:text-gray-600 truncate block">{{ item.dataSourceName }}</span>
            </template>
            <template v-else>
              <span class="font-mono font-medium text-gray-900 dark:text-white block">{{ item.name }}</span>
              <div class="flex items-center gap-1 mt-0.5">
                <DataSourceIcon v-if="item.dataSourceType || item.dataSourceIcon" :type="item.dataSourceType" :icon="item.dataSourceIcon" class="h-2.5" />
                <span class="text-[10px] text-gray-500 dark:text-gray-400">{{ item.dataSourceName }}</span>
              </div>
            </template>
          </div>
        </button>
      </div>
    </div>

    <!-- Raw markdown mode (v-show keeps textarea in DOM). Also the fallback
         surface when the WYSIWYG editor failed to initialize. -->
    <textarea
      v-show="mode === 'raw' || editorFailed"
      v-model="rawText"
      :dir="rawDir"
      :readonly="!isEditable"
      class="raw-textarea"
      :placeholder="placeholder || 'Write instructions using markdown...'"
      @input="onRawInput"
    />
  </div>
</template>

<script setup lang="ts">
import { Editor, EditorContent, BubbleMenu, Extension } from '@tiptap/vue-3'
import StarterKit from '@tiptap/starter-kit'
import Mention from '@tiptap/extension-mention'
import Link from '@tiptap/extension-link'
import Image from '@tiptap/extension-image'
import Table from '@tiptap/extension-table'
import TableRow from '@tiptap/extension-table-row'
import TableHeader from '@tiptap/extension-table-header'
import TableCell from '@tiptap/extension-table-cell'
import { Plugin, PluginKey } from '@tiptap/pm/state'
import { Decoration, DecorationSet } from '@tiptap/pm/view'
import MarkdownIt from 'markdown-it'
import DiffMatchPatch from 'diff-match-patch'
import { useI18n } from 'vue-i18n'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import { firstStrongDir, RTL_LOCALES } from '~/utils/textDirection'
import {
  buildMentionMatcher,
  replaceMentions,
  mentionNeedsQuotes,
  EMPTY_MENTION_MATCHER,
  type MentionMatcher,
} from '~/utils/mentions'

interface MentionItem {
  id: string
  type: 'instruction' | 'metadata_resource' | 'datasource_table' | 'connection_tool'
  name: string | null
  textPreview: string | null
  dataSourceId: string | null
  dataSourceName: string | null
  dataSourceType: string | null
  dataSourceIcon: string | null
}

const props = defineProps<{
  modelValue: string
  mode?: 'wysiwyg' | 'raw'
  placeholder?: string
  dataSourceIds?: string[]
  isAllDataSources?: boolean
  editable?: boolean
  /** Extra mentionable display names, when the host already has them loaded. */
  knownNames?: string[]
}>()

const isEditable = computed(() => props.editable !== false)

const emit = defineEmits<{
  'update:modelValue': [value: string]
  'mention-selected': [item: MentionItem]
}>()

// Auth/config captured in component context for use in async suggestion items handler
const config = useRuntimeConfig()
const { token } = useAuth()
const { organization } = useOrganization()

// ─── Markdown ↔ Tiptap conversion ────────────────────────────────────────────

const md = new MarkdownIt({ html: true, breaks: false, linkify: false })

function convertMentions(text: string): string {
  return replaceMentions(text, mentionMatcher.value, (label) => {
    const safe = label.replace(/&/g, '&amp;').replace(/"/g, '&quot;')
    return `<span data-type="mention" data-id="${safe}" data-label="${safe}"></span>`
  })
}

// Convert @mentions to mention spans, but NEVER inside code (fenced blocks or
// inline `code`). markdown-it escapes any HTML it finds in code, so a mention
// injected there would render as a literal "<span …>" in a code box (and then
// round-trip back into the stored text). Tokenize so code regions pass verbatim.
function preprocessMentions(text: string): string {
  const TOKEN = /(```[\s\S]*?```|~~~[\s\S]*?~~~|`+[^`]*`+)/g
  return text.split(TOKEN).map((seg, i) => (i % 2 === 1 ? seg : convertMentions(seg))).join('')
}

// Repair: a literal mention-span in the stored markdown is never authored — it's
// residue from a prior bad round-trip (often wrapped in backticks). Restore it to
// @label so it renders as a chip again instead of escaped HTML.
function normalizeMentionHtml(text: string): string {
  const toAt = (raw: string): string => {
    const label = (raw || '').replace(/&quot;/g, '"').replace(/&amp;/g, '&').trim()
    return mentionNeedsQuotes(label) ? `@"${label.replace(/"/g, '')}"` : `@${label}`
  }
  return text
    .replace(/`?\s*<span[^>]*data-type=["']mention["'][^>]*data-label=["']([^"']*)["'][^>]*>\s*<\/span>\s*`?/g, (_, l) => toAt(l))
    .replace(/`?\s*<span[^>]*data-type=["']mention["'][^>]*data-id=["']([^"']*)["'][^>]*>\s*<\/span>\s*`?/g, (_, l) => toAt(l))
    .replace(/`?\s*<span[^>]*data-type=["']mention["'][^>]*>([^<]*)<\/span>\s*`?/g, (_, l) => toAt(l))
}

function markdownToHtml(text: string): string {
  if (!text?.trim()) return ''
  const preprocessed = preprocessMentions(normalizeMentionHtml(text))
  // Parse the document as ONE Markdown stream. Splitting on blank lines is not
  // syntax-aware: blank lines are legal inside fenced code blocks, and treating
  // each fragment as a standalone document made markdown-it auto-close and
  // reopen the fence. One small edit could therefore inject hundreds of empty
  // ``` blocks into the stored instruction.
  return md.render(preprocessed)
}

function serializeInlineMarks(text: string, marks: any[]): string {
  for (const mark of (marks || [])) {
    switch (mark.type) {
      case 'bold': text = `**${text}**`; break
      case 'italic': text = `_${text}_`; break
      case 'code': text = `\`${text}\``; break
      case 'strike': text = `~~${text}~~`; break
      case 'link': text = `[${text}](${mark.attrs?.href || ''})`; break
    }
  }
  return text
}

const isListNode = (node: any): boolean =>
  node?.type === 'bulletList' || node?.type === 'orderedList'

const prefixLines = (text: string, prefix: string): string =>
  text.split('\n').map((line) => (prefix + line).trimEnd()).join('\n')

// Everything after an item's first line has to sit at the item's CONTENT
// column, which is the width of its own marker — 2 for `- `, but 3 for `3. `
// and 4 for `10. `. Indent a nested list by less than that and it is no longer
// inside the item at all: the parser reads it as a new top-level list and
// splits the parent list in two around it.
const indentContinuation = (text: string, width: number): string => {
  const pad = ' '.repeat(width)
  return text
    .split('\n')
    .map((line, i) => (i === 0 ? line : (pad + line).trimEnd()))
    .join('\n')
}

function serializeListWith(node: any, marker: (index: number) => string): string {
  return (node.content || [])
    .map((item: any, i: number) => {
      const m = marker(i)
      return m + indentContinuation(serializeListItem(item), m.length)
    })
    .join('\n')
}

// A table cell holds block content, but a GFM row is one line: flatten to a
// single line and escape pipes so the cell boundary stays unambiguous.
function serializeTableCell(cell: any): string {
  return (cell?.content || [])
    .map((n: any) => serializeNode(n))
    .join(' ')
    .replace(/\|/g, '\\|')
    .replace(/\s*\n+\s*/g, ' ')
    .trim()
}

function serializeTable(node: any): string {
  const rows = (node.content || []).filter((r: any) => r?.type === 'tableRow')
  if (!rows.length) return ''
  const cellsOf = (row: any) => (row.content || []).map(serializeTableCell)
  const grid = rows.map(cellsOf)
  // Ragged rows are legal in ProseMirror but not in GFM — pad every row to the
  // widest one so the delimiter row's column count always matches.
  const width = Math.max(...grid.map((r: string[]) => r.length))
  const line = (cells: string[]) =>
    '| ' + Array.from({ length: width }, (_, i) => cells[i] ?? '').join(' | ') + ' |'
  return [
    line(grid[0]),
    '| ' + Array.from({ length: width }, () => '---').join(' | ') + ' |',
    ...grid.slice(1).map(line),
  ].join('\n')
}

function serializeNode(node: any): string {
  if (!node) return ''
  switch (node.type) {
    case 'doc':
      return (node.content || []).map((n: any) => serializeNode(n)).join('\n\n').trim()
    case 'paragraph':
      if (!node.content?.length) return ''
      return (node.content || []).map((n: any) => serializeNode(n)).join('')
    case 'heading': {
      const level = node.attrs?.level || 1
      const inner = (node.content || []).map((n: any) => serializeNode(n)).join('')
      return '#'.repeat(level) + ' ' + inner
    }
    case 'bulletList':
      return serializeListWith(node, () => '- ')
    case 'orderedList': {
      // A list authored as `5.` keeps its offset instead of being renumbered.
      const start = Number(node.attrs?.start) || 1
      return serializeListWith(node, (i) => `${start + i}. `)
    }
    case 'listItem':
      return serializeListItem(node)
    case 'blockquote': {
      // `>` belongs on every LINE, not every child node: a child that
      // serializes to multiple lines (a list, a fence, two paragraphs) would
      // otherwise leave the quote after its first line.
      const inner = (node.content || []).map((n: any) => serializeNode(n)).join('\n\n')
      return prefixLines(inner, '> ')
    }
    case 'codeBlock': {
      const lang = node.attrs?.language || ''
      // The renderer emits a trailing newline inside <code>, so it comes back
      // as part of the text node. Re-adding one on top of it would push a blank
      // line into the fence — and another on every save after that.
      const code = (node.content || []).map((n: any) => n.text || '').join('').replace(/\n$/, '')
      return '```' + lang + '\n' + code + '\n```'
    }
    case 'horizontalRule':
      return '---'
    case 'image': {
      const src = node.attrs?.src || ''
      const alt = node.attrs?.alt || ''
      const title = node.attrs?.title
      return `![${alt}](${src}${title ? ` "${title}"` : ''})`
    }
    case 'table':
      return serializeTable(node)
    case 'tableRow':
    case 'tableHeader':
    case 'tableCell':
      return serializeTableCell(node)
    case 'hardBreak':
      // A bare newline re-parses as a soft break and gets folded away (the
      // renderer runs with breaks:false). The backslash form survives the
      // round-trip, and unlike two trailing spaces it also survives the
      // per-line trimming that quoting and list indentation apply.
      return '\\\n'
    case 'mention': {
      const label = node.attrs?.label || node.attrs?.id || ''
      // Quote names that a bare parse could not delimit, so the markdown stays
      // unambiguous even for a reader without the mention dictionary.
      return mentionNeedsQuotes(label) ? `@"${label}"` : `@${label}`
    }
    case 'text':
      return serializeInlineMarks(node.text || '', node.marks || [])
    default:
      return (node.content || []).map((n: any) => serializeNode(n)).join('')
  }
}

// A list item's children, unindented — the caller indents the whole block to
// the item's content column. A nested list hangs directly off the line above it
// (a tight list); any other following block needs the blank line that separates
// two blocks, or the two would re-parse as one paragraph.
function serializeListItem(node: any): string {
  const children = node.content || []
  let out = ''
  children.forEach((child: any, i: number) => {
    if (i > 0) out += isListNode(child) ? '\n' : '\n\n'
    out += serializeNode(child)
  })
  return out
}

function docToMarkdown(doc: any): string {
  return serializeNode(doc)
}

// Keep the exact source Markdown alongside TipTap's normalized representation.
// On each editor update, apply only the normalized delta to the source. This
// preserves untouched whitespace/fence formatting instead of reserializing the
// entire instruction because the user changed two words.
let sourceMarkdown = props.modelValue || ''
let lastEditorMarkdown: string | null = null

// Constructs whose disappearance between two revisions means the editor could
// not represent them, not that the user deleted them. Counted, not compared, so
// an edit that legitimately removes one row still registers as a smaller count
// on that probe alone.
const STRUCTURE_PROBES: RegExp[] = [
  /^[^\S\n]*\|.*\|[^\S\n]*$/gm,          // table row
  /^[^\S\n]*(?:-{3,}|\*{3,}|_{3,})[^\S\n]*$/gm, // thematic break
  /!\[[^\]]*\]\([^)]*\)/g,               // image
  /\[[^\]]*\]\([^)]*\)/g,                // link (and image, counted by both)
  /^[^\S\n]*(?:```|~~~)/gm,              // code fence
]

const countStructures = (text: string): number[] =>
  STRUCTURE_PROBES.map((re) => (text.match(re) || []).length)

// True when `candidate` holds fewer of some construct than `reference` does.
function dropsStructure(candidate: string, reference: string): boolean {
  const c = countStructures(candidate)
  const r = countStructures(reference)
  return c.some((n, i) => n < r[i])
}

function preserveSourceFormatting(previous: string, next: string): string {
  if (previous === next) return sourceMarkdown
  const dmp = new (DiffMatchPatch as any)()
  const patches = dmp.patch_make(previous, next)
  const [patched, applied] = dmp.patch_apply(patches, sourceMarkdown)
  if (applied.every(Boolean)) return patched
  // Not every hunk landed. `next` is TipTap's serialization of the WHOLE
  // document, so taking it wholesale also writes away anything the schema could
  // not represent — which is how a single unsupported construct used to flatten
  // an entire instruction. Prefer the partially patched source whenever it
  // keeps at least as much structure as `next` would.
  if (applied.some(Boolean) && !dropsStructure(patched, next)) return patched
  return next
}

// ─── Mention fetching ─────────────────────────────────────────────────────────

async function fetchMentionSuggestions(query: string): Promise<MentionItem[]> {
  try {
    const params = new URLSearchParams()
    if (query) params.set('q', query)
    params.set('types', 'instruction,datasource_table,metadata_resource,connection_tool')
    if (!props.isAllDataSources && props.dataSourceIds?.length) {
      params.set('data_source_filter', props.dataSourceIds.join(','))
    }
    const data = await $fetch<any[]>(
      `${config.public.baseURL}/instructions/available-references?${params}`,
      {
        headers: {
          Authorization: token.value || '',
          'X-Organization-Id': organization.value?.id || '',
        }
      }
    )
    const grouped = {
      instruction: (data || []).filter(i => i.type === 'instruction').slice(0, 3),
      table: (data || []).filter(i => i.type === 'datasource_table' || i.type === 'metadata_resource').slice(0, 3),
      tool: (data || []).filter(i => i.type === 'connection_tool').slice(0, 3),
    }
    return [...grouped.instruction, ...grouped.table, ...grouped.tool].map(item => ({
      id: item.id,
      type: item.type as MentionItem['type'],
      name: item.name || null,
      textPreview: item.text_preview || null,
      dataSourceId: item.data_source_id || null,
      dataSourceName: item.data_source_name || null,
      dataSourceType: item.data_source_type || null,
      dataSourceIcon: item.data_source_icon ?? item.icon ?? null,
    }))
  } catch {
    return []
  }
}

// ─── Mention dictionary ───────────────────────────────────────────────────────
// Stored markdown holds display names, not ids, so turning `@Sales Orders` back
// into one chip needs the set of names that exist. Fetched once per editor (the
// same endpoint the typeahead uses, unfiltered) and held as a trie.

const mentionMatcher = shallowRef<MentionMatcher>(EMPTY_MENTION_MATCHER)

async function loadMentionDictionary() {
  const seeded = props.knownNames?.length ? props.knownNames : null
  if (seeded) mentionMatcher.value = buildMentionMatcher(seeded.map(name => ({ name })))
  try {
    const params = new URLSearchParams()
    params.set('types', 'instruction,datasource_table,metadata_resource,connection_tool')
    if (!props.isAllDataSources && props.dataSourceIds?.length) {
      params.set('data_source_filter', props.dataSourceIds.join(','))
    }
    const data = await $fetch<any[]>(
      `${config.public.baseURL}/instructions/available-references?${params}`,
      {
        headers: {
          Authorization: token.value || '',
          'X-Organization-Id': organization.value?.id || '',
        }
      }
    )
    if (!data?.length) return
    mentionMatcher.value = buildMentionMatcher([
      ...data,
      ...(seeded || []).map(name => ({ name })),
    ])
  } catch {
    // Keep whatever we have; the parser degrades to identifier-shaped mentions.
  }
}

// Re-chip once the dictionary lands. Only while unfocused: re-setting content
// under the caret would move it mid-typing, and an editor being typed into has
// already been chipped by the suggestion plugin anyway.
watch(mentionMatcher, () => {
  const e = editor.value
  if (!e || e.isFocused) return
  const currentMd = docToMarkdown(e.getJSON())
  const rechipped = markdownToHtml(currentMd)
  if (rechipped !== markdownToHtmlCache) {
    markdownToHtmlCache = rechipped
    e.commands.setContent(rechipped, false)
  }
})
let markdownToHtmlCache = ''

// ─── Mention suggestion state ─────────────────────────────────────────────────

const containerRef = ref<HTMLElement | null>(null)
const dropdownRef = ref<HTMLElement | null>(null)

const mentionState = ref<{
  active: boolean
  items: MentionItem[]
  command: ((attrs: { id: string; label: string }) => void) | null
  selectedIndex: number
  query: string
  position: { top: number; left: number }
}>({
  active: false,
  items: [],
  command: null,
  selectedIndex: 0,
  query: '',
  position: { top: 0, left: 0 },
})

function getDropdownPosition(clientRect: DOMRect | null): { top: number; left: number } {
  if (!clientRect || !containerRef.value) return { top: 0, left: 0 }
  const cr = containerRef.value.getBoundingClientRect()
  return {
    top: clientRect.bottom - cr.top + 4,
    left: Math.min(Math.max(0, clientRect.left - cr.left), (cr.width || 300) - 300),
  }
}

function selectMentionItem(item: MentionItem) {
  if (!mentionState.value.command) return
  const label = item.name || (item.textPreview ? item.textPreview.slice(0, 30) + '...' : item.id)
  mentionState.value.command({ id: item.id, label })
  emit('mention-selected', item)
  mentionState.value.active = false
}

// Scroll highlighted item into view in dropdown
function scrollDropdownItem(index: number) {
  nextTick(() => {
    if (!dropdownRef.value) return
    const el = dropdownRef.value.querySelector(`[data-idx="${index}"]`) as HTMLElement | null
    if (!el) return
    const ct = dropdownRef.value.scrollTop
    const cb = ct + dropdownRef.value.clientHeight
    if (el.offsetTop < ct) dropdownRef.value.scrollTop = el.offsetTop
    else if (el.offsetTop + el.offsetHeight > cb) dropdownRef.value.scrollTop = el.offsetTop + el.offsetHeight - dropdownRef.value.clientHeight
  })
}

// ─── Auto text direction ──────────────────────────────────────────────────────
// Per-block direction from each block's first strong character — the same
// behavior as chat markdown (`<p dir="auto">`) and the PromptBoxV2 input, so
// Hebrew/Arabic instructions right-align while English blocks (and code, which
// is excluded) stay LTR. Applied as ProseMirror node decorations: the rendered
// DOM gets a `dir` attribute but the document itself is untouched, so nothing
// leaks into the serialized markdown.
// Mirrors InstructionText's DIR_OPEN_TOKENS — the two lists must agree, or the
// editor and the read-only view disagree about which way a block reads. Tables
// matter most: with no dir of its own a table inherits the document's LTR and
// lays an RTL table's columns out backwards.
const AUTO_DIR_NODES = new Set([
  'paragraph', 'heading', 'bulletList', 'orderedList', 'listItem', 'blockquote',
  'table', 'tableRow', 'tableHeader', 'tableCell',
])

const AutoDir = Extension.create({
  name: 'autoDir',
  addProseMirrorPlugins() {
    return [
      new Plugin({
        key: new PluginKey('instructionAutoDir'),
        props: {
          decorations(state) {
            const decos: Decoration[] = []
            state.doc.descendants((node, pos) => {
              if (!AUTO_DIR_NODES.has(node.type.name)) return
              const dir = firstStrongDir(node.textContent)
              if (dir) decos.push(Decoration.node(pos, pos + node.nodeSize, { dir }))
            })
            return DecorationSet.create(state.doc, decos)
          },
        },
      }),
    ]
  },
})

// While the editor is empty there is no strong character to derive direction
// from, so an RTL placeholder would left-align (same problem MentionInput
// solves). Fall back to the UI locale direction while empty; once there is
// content, the per-block decorations / textarea dir="auto" take over.
const { locale: i18nLocale } = useI18n({ useScope: 'global' })
const emptyDir = computed(() => (RTL_LOCALES.has(String(i18nLocale.value)) ? 'rtl' : 'ltr'))
const containerDir = computed(() => (props.modelValue?.trim() ? undefined : emptyDir.value))

// ─── Editor setup ─────────────────────────────────────────────────────────────

let skipPropWatch = false

// Constructed manually (instead of useEditor) so an initialization failure —
// e.g. a dependency-level ProseMirror error — degrades to the raw-markdown
// textarea instead of silently rendering an empty panel.
const editor = shallowRef<InstanceType<typeof Editor> | undefined>()
const editorFailed = ref(false)

const editorOptions = () => ({
  extensions: [
    StarterKit.configure({
      // All six levels: markdown allows `####`-`######`, and a level missing
      // from the schema is parsed as a plain paragraph — which then serializes
      // back without its `#` marks, demoting the heading permanently.
      heading: { levels: [1, 2, 3, 4, 5, 6] },
    }),
    // Every markdown construct md.render() can emit needs a matching schema
    // node, or ProseMirror silently drops it on parse and docToMarkdown has
    // nothing left to serialize — the construct is then deleted from the
    // stored instruction on the next save. StarterKit covers paragraphs,
    // headings, lists, code, quotes and rules; these cover the rest.
    Link.configure({ openOnClick: false, autolink: false, HTMLAttributes: { rel: 'noopener noreferrer nofollow' } }),
    // `inline` matches how the renderer emits images — always inside a
    // paragraph, never as a sibling of one. As a block node the image forces
    // ProseMirror to split the paragraph around it on parse, and the node can
    // be dropped outright depending on what surrounds it.
    Image.configure({ inline: true, allowBase64: true }),
    // `resizable` is what installs tiptap's TableView, and with it the
    // `.tableWrapper` scroll container — without it a table wider than the
    // panel has nowhere to go and every cell wraps to breaking point.
    Table.configure({ resizable: true }),
    TableRow,
    TableHeader,
    TableCell,
    Mention.configure({
      HTMLAttributes: { class: 'mention-chip' },
      renderLabel: ({ node }: any) => `@${node.attrs.label ?? node.attrs.id}`,
      suggestion: {
        char: '@',
        allowSpaces: false,
        items: async ({ query }: { query: string }) => {
          return fetchMentionSuggestions(query)
        },
        render: () => ({
          onStart: (suggProps: any) => {
            mentionState.value = {
              active: true,
              items: suggProps.items || [],
              command: suggProps.command,
              selectedIndex: 0,
              query: suggProps.query || '',
              position: getDropdownPosition(suggProps.clientRect?.()),
            }
          },
          onUpdate: (suggProps: any) => {
            Object.assign(mentionState.value, {
              items: suggProps.items || [],
              command: suggProps.command,
              query: suggProps.query || '',
              position: getDropdownPosition(suggProps.clientRect?.()),
              selectedIndex: 0,
            })
          },
          onExit: () => {
            mentionState.value.active = false
          },
          onKeyDown: ({ event }: { event: KeyboardEvent }) => {
            if (!mentionState.value.active) return false
            const total = mentionState.value.items.length
            if (event.key === 'ArrowDown') {
              mentionState.value.selectedIndex = Math.min(mentionState.value.selectedIndex + 1, total - 1)
              scrollDropdownItem(mentionState.value.selectedIndex)
              return true
            }
            if (event.key === 'ArrowUp') {
              mentionState.value.selectedIndex = Math.max(mentionState.value.selectedIndex - 1, 0)
              scrollDropdownItem(mentionState.value.selectedIndex)
              return true
            }
            if (event.key === 'Enter') {
              const item = mentionState.value.items[mentionState.value.selectedIndex]
              if (item) selectMentionItem(item)
              return true
            }
            if (event.key === 'Escape') {
              mentionState.value.active = false
              return true
            }
            return false
          },
        }),
      },
    }),
    AutoDir,
  ],
  content: markdownToHtml(props.modelValue),
  editorProps: {
    attributes: { class: 'tiptap-prose' },
  },
  onUpdate: ({ editor: e }: { editor: any }) => {
    if (!isEditable.value) return
    const normalized = docToMarkdown(e.getJSON())
    const mdText = lastEditorMarkdown === null
      ? normalized
      : preserveSourceFormatting(lastEditorMarkdown, normalized)
    sourceMarkdown = mdText
    lastEditorMarkdown = normalized
    skipPropWatch = true
    emit('update:modelValue', mdText)
  },
})

// Create the editor on mount (same lifecycle as useEditor), but guarded: if
// construction throws, flip to the raw-markdown fallback so the instruction
// text is still readable/editable instead of a blank pane.
onMounted(() => {
  loadMentionDictionary()
  try {
    editor.value = new Editor(editorOptions() as any)
    lastEditorMarkdown = docToMarkdown(editor.value.getJSON())
    // Apply editable state explicitly, to avoid stale state from HMR / component reuse
    editor.value.setEditable(isEditable.value)
  } catch (e) {
    console.error('[InstructionEditor] editor failed to initialize; falling back to raw markdown', e)
    editorFailed.value = true
    rawText.value = props.modelValue
  }
})

onBeforeUnmount(() => {
  editor.value?.destroy()
  editor.value = undefined
})

watch(isEditable, (val) => {
  editor.value?.setEditable(val)
}, { immediate: true })

// Sync editor when modelValue changes from outside (e.g. Enhance button)
watch(
  () => props.modelValue,
  (newVal) => {
    if (skipPropWatch) {
      skipPropWatch = false
      return
    }
    if (props.mode === 'raw' || editorFailed.value) {
      rawText.value = newVal
      sourceMarkdown = newVal
      return
    }
    if (!editor.value) return
    const currentMd = docToMarkdown(editor.value.getJSON())
    sourceMarkdown = newVal
    if (newVal !== currentMd) {
      editor.value.commands.setContent(markdownToHtml(newVal), false)
      lastEditorMarkdown = docToMarkdown(editor.value.getJSON())
    } else {
      lastEditorMarkdown = currentMd
    }
  }
)

// ─── Raw mode ──────────────────────────────────────────────────────────────────

const rawText = ref(props.modelValue)

const rawDir = computed(() => (rawText.value?.trim() ? 'auto' : emptyDir.value))

// When switching to raw mode, sync rawText from the current editor state
watch(() => props.mode, (newMode, oldMode) => {
  if (newMode === 'raw') {
    rawText.value = editor.value ? docToMarkdown(editor.value.getJSON()) : props.modelValue
  } else if (newMode === 'wysiwyg' && oldMode === 'raw') {
    nextTick(() => {
      if (editor.value) {
        sourceMarkdown = rawText.value
        editor.value.commands.setContent(markdownToHtml(rawText.value), false)
        lastEditorMarkdown = docToMarkdown(editor.value.getJSON())
      }
    })
  }
})

function onRawInput() {
  sourceMarkdown = rawText.value
  emit('update:modelValue', rawText.value)
}
</script>

<style scoped>
.instruction-wysiwyg {
  position: relative;
}

/* Tiptap editor content area */
/* Element typography for `.tiptap-prose` lives in
 * assets/css/instruction-prose.css, shared with InstructionText's
 * `.instruction-prose` so the editor and the read-only view cannot drift
 * apart. Only editor-specific chrome stays here. */

/* Mention chip */
.wysiwyg-content :deep(.mention-chip) {
  background-color: rgba(99, 102, 241, 0.12);
  color: #4338ca;
  border-radius: 4px;
  padding: 1px 4px;
  font-weight: 500;
  font-size: 0.95em;
  white-space: nowrap;
}

/* Bubble toolbar */
.bubble-toolbar {
  display: flex;
  align-items: center;
  gap: 2px;
  background: white;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.1);
  padding: 4px 6px;
}

.bubble-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  min-width: 26px;
  height: 24px;
  padding: 0 4px;
  border-radius: 4px;
  color: #374151;
  font-size: 12px;
  transition: background-color 0.1s;
  cursor: pointer;
}

.bubble-btn:hover {
  background-color: #f3f4f6;
}

.bubble-btn.active {
  background-color: #e0e7ff;
  color: #4338ca;
}

.bubble-sep {
  width: 1px;
  height: 16px;
  background: #e5e7eb;
  margin: 0 2px;
}

/* Raw markdown textarea */
.raw-textarea {
  width: 100%;
  min-height: 210px;
  padding: 8px 0;
  font-family: ui-monospace, monospace;
  font-size: 12px;
  line-height: 1.625;
  color: #111827;
  background: transparent;
  border: none;
  outline: none;
  resize: vertical;
  /* Per-line auto direction while editing raw markdown: Hebrew prose lines
   * read RTL while SQL/code lines stay LTR within the same textarea. */
  unicode-bidi: plaintext;
  text-align: start;
}

.raw-textarea::placeholder {
  color: #9ca3af;
}

/* ─── Dark mode ────────────────────────────────────────────────────────────
   Every color above is a hardcoded light-theme literal, so without these the
   instruction body renders near-black on the dark surface. Palette mirrors
   InstructionText's dark block so the editor and the read-only view match.

   The `.dark` class lives on <html> (Tailwind darkMode: 'class'), outside this
   component's scope, so these are authored as :global and matched by the
   component-unique `.wysiwyg-content` / `.bubble-*` / `.raw-textarea` classes.
   Each pairs with an equal-specificity light rule above and wins by order. */
:global(.dark .wysiwyg-content .mention-chip) {
  background-color: rgba(129, 140, 248, 0.18);
  color: #c7d2fe;
}

/* The bubble toolbar is teleported to <body> by tippy, so it sits outside the
   component subtree — `.dark` on <html> still reaches it. */
:global(.dark .bubble-toolbar) {
  background: #1f2937;
  border-color: #374151;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
}
:global(.dark .bubble-btn) { color: #d1d5db; }
:global(.dark .bubble-btn:hover) { background-color: #374151; }
:global(.dark .bubble-btn.active) { background-color: #3730a3; color: #e0e7ff; }
:global(.dark .bubble-sep) { background: #374151; }

:global(.dark .raw-textarea) { color: #e5e7eb; }
:global(.dark .raw-textarea::placeholder) { color: #6b7280; }
</style>
