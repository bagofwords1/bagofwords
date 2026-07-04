<template>
  <div class="instruction-wysiwyg" ref="containerRef" :dir="containerDir">
    <!-- WYSIWYG mode (v-show keeps EditorContent in DOM so ProseMirror stays attached) -->
    <div v-show="mode === 'wysiwyg'">
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
                <DataSourceIcon v-if="item.dataSourceType" :type="item.dataSourceType" class="h-2.5" />
                <span class="text-[10px] text-gray-500 dark:text-gray-400">{{ item.dataSourceName }}</span>
              </div>
            </template>
          </div>
        </button>
      </div>
    </div>

    <!-- Raw markdown mode (v-show keeps textarea in DOM) -->
    <textarea
      v-show="mode === 'raw'"
      v-model="rawText"
      :dir="rawDir"
      class="raw-textarea"
      :placeholder="placeholder || 'Write instructions using markdown...'"
      @input="onRawInput"
    />
  </div>
</template>

<script setup lang="ts">
import { useEditor, EditorContent, BubbleMenu, Extension } from '@tiptap/vue-3'
import StarterKit from '@tiptap/starter-kit'
import Mention from '@tiptap/extension-mention'
import { Plugin } from '@tiptap/pm/state'
import { Decoration, DecorationSet } from '@tiptap/pm/view'
import MarkdownIt from 'markdown-it'
import { useI18n } from 'vue-i18n'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import { firstStrongDir, RTL_LOCALES } from '~/utils/textDirection'

interface MentionItem {
  id: string
  type: 'instruction' | 'metadata_resource' | 'datasource_table' | 'connection_tool'
  name: string | null
  textPreview: string | null
  dataSourceId: string | null
  dataSourceName: string | null
  dataSourceType: string | null
}

const props = defineProps<{
  modelValue: string
  mode?: 'wysiwyg' | 'raw'
  placeholder?: string
  dataSourceIds?: string[]
  isAllDataSources?: boolean
  editable?: boolean
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
  return text.replace(
    /@([A-Za-z_][A-Za-z0-9_]*(?:[.\-][A-Za-z0-9_]+)*|"[^"]+")/g,
    (_, captured) => {
      const label = captured.startsWith('"') && captured.endsWith('"')
        ? captured.slice(1, -1)
        : captured
      const safe = label.replace(/&/g, '&amp;').replace(/"/g, '&quot;')
      return `<span data-type="mention" data-id="${safe}" data-label="${safe}"></span>`
    }
  )
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
    return /[\s\-."]/.test(label) ? `@"${label.replace(/"/g, '')}"` : `@${label}`
  }
  return text
    .replace(/`?\s*<span[^>]*data-type=["']mention["'][^>]*data-label=["']([^"']*)["'][^>]*>\s*<\/span>\s*`?/g, (_, l) => toAt(l))
    .replace(/`?\s*<span[^>]*data-type=["']mention["'][^>]*data-id=["']([^"']*)["'][^>]*>\s*<\/span>\s*`?/g, (_, l) => toAt(l))
    .replace(/`?\s*<span[^>]*data-type=["']mention["'][^>]*>([^<]*)<\/span>\s*`?/g, (_, l) => toAt(l))
}

function markdownToHtml(text: string): string {
  if (!text?.trim()) return ''
  const preprocessed = preprocessMentions(normalizeMentionHtml(text))
  // Render block-by-block (split on paragraph breaks) so runs of blank lines are
  // preserved as empty paragraphs. md.render() alone collapses any number of
  // blank lines into a single paragraph break; the serializer joins paragraphs
  // with "\n\n", so an empty paragraph round-trips back to a blank line.
  const blocks = preprocessed.split('\n\n')
  return blocks.map((b) => (b.length === 0 ? '<p></p>' : md.render(b))).join('')
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

function serializeNode(node: any): string {
  if (!node) return ''
  switch (node.type) {
    case 'doc':
      return (node.content || []).map(serializeNode).join('\n\n').trim()
    case 'paragraph':
      if (!node.content?.length) return ''
      return (node.content || []).map(serializeNode).join('')
    case 'heading': {
      const level = node.attrs?.level || 1
      const inner = (node.content || []).map(serializeNode).join('')
      return '#'.repeat(level) + ' ' + inner
    }
    case 'bulletList':
      return (node.content || []).map((item: any) => '- ' + serializeListItem(item)).join('\n')
    case 'orderedList':
      return (node.content || []).map((item: any, i: number) => `${i + 1}. ` + serializeListItem(item)).join('\n')
    case 'listItem':
      return serializeListItem(node)
    case 'blockquote':
      return (node.content || []).map(serializeNode).map((s: string) => '> ' + s).join('\n')
    case 'codeBlock': {
      const lang = node.attrs?.language || ''
      const code = (node.content || []).map((n: any) => n.text || '').join('')
      return '```' + lang + '\n' + code + '\n```'
    }
    case 'hardBreak':
      return '\n'
    case 'mention': {
      const label = node.attrs?.label || node.attrs?.id || ''
      return /[\s\-.]/.test(label) ? `@"${label}"` : `@${label}`
    }
    case 'text':
      return serializeInlineMarks(node.text || '', node.marks || [])
    default:
      return (node.content || []).map(serializeNode).join('')
  }
}

function serializeListItem(node: any): string {
  return (node.content || []).map(serializeNode).join('\n')
}

function docToMarkdown(doc: any): string {
  return serializeNode(doc)
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
    }))
  } catch {
    return []
  }
}

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
const AUTO_DIR_NODES = new Set(['paragraph', 'heading', 'bulletList', 'orderedList', 'listItem', 'blockquote'])

const AutoDir = Extension.create({
  name: 'autoDir',
  addProseMirrorPlugins() {
    return [
      new Plugin({
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

const editor = useEditor({
  extensions: [
    StarterKit.configure({
      heading: { levels: [1, 2, 3] },
    }),
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
  onUpdate: ({ editor: e }) => {
    if (!isEditable.value) return
    const mdText = docToMarkdown(e.getJSON())
    skipPropWatch = true
    emit('update:modelValue', mdText)
  },
})

// Apply editable state after mount — always explicit, to avoid stale state from HMR / component reuse
onMounted(() => {
  editor.value?.setEditable(isEditable.value)
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
    if (props.mode === 'raw') {
      rawText.value = newVal
      return
    }
    if (!editor.value) return
    const currentMd = docToMarkdown(editor.value.getJSON())
    if (newVal !== currentMd) {
      editor.value.commands.setContent(markdownToHtml(newVal), false)
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
        editor.value.commands.setContent(markdownToHtml(rawText.value), false)
      }
    })
  }
})

function onRawInput() {
  emit('update:modelValue', rawText.value)
}
</script>

<style scoped>
.instruction-wysiwyg {
  position: relative;
}

/* Tiptap editor content area */
.wysiwyg-content :deep(.tiptap-prose) {
  min-height: 210px;
  padding: 8px 0;
  font-size: 12px;
  line-height: 1.625;
  color: #111827;
  outline: none;
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
  /* `start` resolves against each block's own dir (set by the auto-dir
   * decorations), so RTL blocks right-align and LTR blocks left-align. */
  text-align: start;
}

.wysiwyg-content :deep(.tiptap-prose:focus) {
  outline: none;
}

/* Headings */
.wysiwyg-content :deep(.tiptap-prose h1) { font-size: 1.25em; font-weight: 600; margin: 0.75em 0 0.25em; color: #111827; }
.wysiwyg-content :deep(.tiptap-prose h2) { font-size: 1.1em; font-weight: 600; margin: 0.6em 0 0.2em; color: #111827; }
.wysiwyg-content :deep(.tiptap-prose h3) { font-size: 1em; font-weight: 600; margin: 0.5em 0 0.15em; color: #111827; }

/* Paragraphs */
.wysiwyg-content :deep(.tiptap-prose p) { margin-bottom: 0.5em; }
.wysiwyg-content :deep(.tiptap-prose p:last-child) { margin-bottom: 0; }

/* Lists — logical padding so RTL lists get their markers on the right edge */
.wysiwyg-content :deep(.tiptap-prose ul) { padding-inline-start: 1.25em; list-style: disc; margin-bottom: 0.5em; }
.wysiwyg-content :deep(.tiptap-prose ol) { padding-inline-start: 1.25em; list-style: decimal; margin-bottom: 0.5em; }
.wysiwyg-content :deep(.tiptap-prose li) { margin-bottom: 0.2em; }

/* Inline code */
.wysiwyg-content :deep(.tiptap-prose code) {
  background: #f3f4f6;
  padding: 1px 4px;
  border-radius: 3px;
  font-family: ui-monospace, monospace;
  font-size: 0.9em;
  color: #374151;
}

/* Code blocks — always LTR regardless of the surrounding text direction
 * (same policy as rtl.css: SQL / identifiers read left-to-right). */
.wysiwyg-content :deep(.tiptap-prose pre) {
  background: #f9fafb;
  padding: 10px 12px;
  border-radius: 6px;
  margin-bottom: 0.5em;
  overflow-x: auto;
  direction: ltr;
  unicode-bidi: isolate;
  text-align: left;
}
.wysiwyg-content :deep(.tiptap-prose pre code) {
  background: none;
  padding: 0;
  font-size: 11px;
  line-height: 1.5;
}

/* Blockquote — logical border/padding so the bar sits on the start edge */
.wysiwyg-content :deep(.tiptap-prose blockquote) {
  border-inline-start: 3px solid #e5e7eb;
  padding-inline-start: 1em;
  margin: 0.5em 0;
  color: #6b7280;
}

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
</style>
