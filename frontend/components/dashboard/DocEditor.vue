<template>
  <div class="doc-editor h-full flex flex-col bg-white dark:bg-gray-900">
    <!-- Editor toolbar -->
    <div class="flex-shrink-0 flex items-center gap-1 px-4 py-2 border-b border-gray-100 dark:border-gray-800">
      <template v-if="editor">
        <button
          v-for="btn in toolbarButtons"
          :key="btn.title"
          :title="btn.title"
          class="p-1.5 rounded text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
          :class="{ 'bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-gray-100': btn.isActive() }"
          @mousedown.prevent="btn.run()"
        >
          <Icon :name="btn.icon" class="w-4 h-4" />
        </button>
      </template>

      <div class="ms-auto flex items-center gap-1.5">
        <span v-if="saveError" class="text-xs text-red-500 max-w-xs truncate" :title="saveError">{{ saveError }}</span>

        <!-- Export actions (moved here from the top bar) -->
        <button
          :title="$t('docViewer.exportMarkdown')"
          class="flex items-center gap-1 px-2 py-1.5 rounded text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
          @mousedown.prevent="exportMarkdown"
        >
          <Icon name="heroicons:arrow-down-tray" class="w-3.5 h-3.5" />
          <span class="text-xs font-medium">.md</span>
        </button>
        <button
          :title="$t('docViewer.exportPdf')"
          class="flex items-center gap-1 px-2 py-1.5 rounded text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
          @mousedown.prevent="exportPdf"
        >
          <Icon name="heroicons:document-arrow-down" class="w-3.5 h-3.5" />
          <span class="text-xs font-medium">{{ $t('docViewer.pdf') }}</span>
        </button>

        <span class="mx-0.5 h-4 w-px bg-gray-200 dark:bg-gray-700"></span>

        <button
          class="px-3 py-1.5 text-xs font-medium text-white bg-blue-600 rounded-md hover:bg-blue-700 disabled:opacity-50 transition-colors flex items-center gap-1.5"
          :disabled="isSaving"
          @click="save"
        >
          <Spinner v-if="isSaving" class="w-3 h-3" />
          {{ $t('docEditor.save') }}
        </button>
      </div>
    </div>

    <!-- Editor content -->
    <div class="flex-1 min-h-0 overflow-y-auto">
      <EditorContent :editor="editor" class="bow-doc-editor" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, shallowRef, computed } from 'vue'
import { Editor, EditorContent, VueNodeViewRenderer } from '@tiptap/vue-3'
import StarterKit from '@tiptap/starter-kit'
import Table from '@tiptap/extension-table'
import TableRow from '@tiptap/extension-table-row'
import TableCell from '@tiptap/extension-table-cell'
import TableHeader from '@tiptap/extension-table-header'
import { Node, mergeAttributes } from '@tiptap/core'
import { Markdown } from 'tiptap-markdown'
import DocVizNodeView from '~/components/dashboard/DocVizNodeView.vue'
import { detectDocDir } from '~/utils/docDirection'

interface DocViz {
  id: string
  title?: string
  view?: any
  rows?: any[]
  columns?: any[]
  dataModel?: any
  stepStatus?: string
}

const props = defineProps<{
  markdown: string
  visualizations?: DocViz[]
  title?: string
}>()

const emit = defineEmits<{
  (e: 'save', markdown: string): void
  (e: 'cancel'): void
}>()

const isSaving = ref(false)
const saveError = ref('')

// Exposed so the parent can toggle the saving state / surface API errors
defineExpose({
  setSaving(v: boolean) { isSaving.value = v },
  setError(msg: string) { saveError.value = msg },
})

const vizMap = computed(() => {
  const m = new Map<string, DocViz>()
  for (const v of props.visualizations || []) m.set(String(v.id).toLowerCase(), v)
  return m
})

/**
 * VizEmbed — atom block node for {{viz:<uuid>}} placeholders.
 * Loading: markdown is pre-processed so placeholders become <viz-embed> tags
 * (fence-aware), which parseHTML picks up. Saving: the markdown serializer
 * writes the placeholder back, so the round-trip is exact.
 */
const VizEmbed = Node.create({
  name: 'vizEmbed',
  group: 'block',
  atom: true,
  selectable: true,
  draggable: true,

  addAttributes() {
    return { vizId: { default: null, parseHTML: (el: HTMLElement) => el.getAttribute('data-viz-id') } }
  },
  parseHTML() {
    return [{ tag: 'viz-embed[data-viz-id]' }]
  },
  renderHTML({ HTMLAttributes }) {
    return ['viz-embed', mergeAttributes({ 'data-viz-id': HTMLAttributes.vizId })]
  },
  addNodeView() {
    return VueNodeViewRenderer(DocVizNodeView)
  },
  addStorage() {
    return {
      markdown: {
        serialize(state: any, node: any) {
          state.write(`{{viz:${node.attrs.vizId}}}`)
          state.closeBlock(node)
        },
      },
    }
  },
})

const FENCE_RE = /^\s*(```|~~~)/
const VIZ_RE = /^\s*\{\{\s*viz:\s*([0-9a-fA-F-]{8,64})\s*\}\}\s*$/

/** Replace whole-line viz placeholders with <viz-embed> tags, skipping code fences. */
function preprocessForEditor(markdown: string): string {
  const lines = (markdown || '').split('\n')
  const out: string[] = []
  let fenceMarker: string | null = null
  for (const line of lines) {
    const fence = line.match(FENCE_RE)
    if (fence) {
      if (fenceMarker === null) fenceMarker = fence[1]
      else if (fence[1] === fenceMarker) fenceMarker = null
      out.push(line)
      continue
    }
    if (fenceMarker === null) {
      const m = line.match(VIZ_RE)
      if (m) {
        out.push(`<viz-embed data-viz-id="${m[1].toLowerCase()}"></viz-embed>`)
        continue
      }
    }
    out.push(line)
  }
  return out.join('\n')
}

const editor = shallowRef<Editor>()

onMounted(() => {
  editor.value = new Editor({
    content: preprocessForEditor(props.markdown),
    extensions: [
      StarterKit,
      Table.configure({ resizable: false }),
      TableRow,
      TableCell,
      TableHeader,
      VizEmbed,
      Markdown.configure({
        html: true,           // needed so <viz-embed> pre-processed tags parse
        tightLists: true,
        linkify: true,
        breaks: false,
        transformPastedText: true,
      }),
    ],
    editorProps: {
      // Direction inferred from the doc content (Hebrew/Arabic → RTL), not the
      // UI locale — a Hebrew doc edited by an English-UI user stays RTL. An
      // explicit dir (never dir="auto") keeps the caret placed correctly.
      attributes: { class: 'focus:outline-none', dir: detectDocDir(props.markdown) },
    },
  })
  // Provide viz data to node views via editor storage
  ;(editor.value.storage as any).docVizMap = vizMap
})

onBeforeUnmount(() => {
  editor.value?.destroy()
})

function currentMarkdown(): string {
  if (!editor.value) return props.markdown
  return (editor.value.storage as any).markdown.getMarkdown()
}

function save() {
  if (!editor.value) return
  saveError.value = ''
  emit('save', currentMarkdown())
}

// Download the current (possibly unsaved) editor content as a .md file.
function exportMarkdown() {
  const title = props.title || 'document'
  const blob = new Blob([currentMarkdown()], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${title.replace(/[^\w\d֐-׿؀-ۿ -]+/g, '').trim() || 'document'}.md`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

// Export as PDF via the browser print dialog. The print stylesheet below
// isolates the editor content (live charts and all) and hides the toolbar/app
// chrome, so the PDF is the document only.
function exportPdf() {
  document.documentElement.classList.add('printing-doc-editor')
  const cleanup = () => {
    document.documentElement.classList.remove('printing-doc-editor')
    window.removeEventListener('afterprint', cleanup)
  }
  window.addEventListener('afterprint', cleanup)
  setTimeout(cleanup, 2000)
  window.print()
}

const toolbarButtons = computed(() => {
  const e = editor.value
  if (!e) return []
  return [
    { title: 'Heading 2', icon: 'heroicons:h2', isActive: () => e.isActive('heading', { level: 2 }), run: () => e.chain().focus().toggleHeading({ level: 2 }).run() },
    { title: 'Heading 3', icon: 'heroicons:h3', isActive: () => e.isActive('heading', { level: 3 }), run: () => e.chain().focus().toggleHeading({ level: 3 }).run() },
    { title: 'Bold', icon: 'heroicons:bold', isActive: () => e.isActive('bold'), run: () => e.chain().focus().toggleBold().run() },
    { title: 'Italic', icon: 'heroicons:italic', isActive: () => e.isActive('italic'), run: () => e.chain().focus().toggleItalic().run() },
    { title: 'Bullet list', icon: 'heroicons:list-bullet', isActive: () => e.isActive('bulletList'), run: () => e.chain().focus().toggleBulletList().run() },
    { title: 'Ordered list', icon: 'heroicons:numbered-list', isActive: () => e.isActive('orderedList'), run: () => e.chain().focus().toggleOrderedList().run() },
    { title: 'Blockquote', icon: 'heroicons:chat-bubble-bottom-center-text', isActive: () => e.isActive('blockquote'), run: () => e.chain().focus().toggleBlockquote().run() },
    { title: 'Code block', icon: 'heroicons:code-bracket', isActive: () => e.isActive('codeBlock'), run: () => e.chain().focus().toggleCodeBlock().run() },
  ]
})
</script>

<style scoped>
.bow-doc-editor {
  max-width: 46rem;
  margin: 0 auto;
  padding: 3rem 1.5rem 5rem;
}
.bow-doc-editor :deep(.ProseMirror) {
  font-size: 0.9375rem;
  line-height: 1.75;
  color: rgb(55 65 81);
  min-height: 50vh;
}
:global(.dark) .bow-doc-editor :deep(.ProseMirror) { color: rgb(209 213 219); }

.bow-doc-editor :deep(h1) { font-size: 1.75rem; font-weight: 700; letter-spacing: -0.02em; margin: 0 0 1.25rem; color: rgb(17 24 39); }
.bow-doc-editor :deep(h2) { font-size: 1.25rem; font-weight: 600; margin: 2.25rem 0 0.75rem; padding-bottom: 0.375rem; border-bottom: 1px solid rgb(243 244 246); color: rgb(17 24 39); }
.bow-doc-editor :deep(h3) { font-size: 1.0625rem; font-weight: 600; margin: 1.75rem 0 0.5rem; color: rgb(31 41 55); }
:global(.dark) .bow-doc-editor :deep(h1),
:global(.dark) .bow-doc-editor :deep(h2) { color: rgb(243 244 246); border-color: rgb(31 41 55); }
:global(.dark) .bow-doc-editor :deep(h3) { color: rgb(229 231 235); }

.bow-doc-editor :deep(p) { margin: 0.75rem 0; }
.bow-doc-editor :deep(ul), .bow-doc-editor :deep(ol) { margin: 0.75rem 0; padding-inline-start: 1.5rem; }
.bow-doc-editor :deep(ul) { list-style: disc; }
.bow-doc-editor :deep(ol) { list-style: decimal; }
.bow-doc-editor :deep(blockquote) { margin: 1.25rem 0; padding-inline-start: 1rem; border-inline-start: 3px solid rgb(229 231 235); color: rgb(107 114 128); font-style: italic; }
.bow-doc-editor :deep(pre) { margin: 1.25rem 0; padding: 1rem; background: rgb(249 250 251); border: 1px solid rgb(243 244 246); border-radius: 0.5rem; font-size: 0.8125rem; overflow-x: auto; }
:global(.dark) .bow-doc-editor :deep(pre) { background: rgb(17 24 39); border-color: rgb(31 41 55); }
.bow-doc-editor :deep(code) { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.8125rem; }

.bow-doc-editor :deep(table) { width: 100%; margin: 1.25rem 0; border-collapse: collapse; font-size: 0.875rem; }
.bow-doc-editor :deep(th) { text-align: start; font-weight: 600; padding: 0.5rem 0.75rem; border-bottom: 2px solid rgb(229 231 235); }
.bow-doc-editor :deep(td) { padding: 0.5rem 0.75rem; border-bottom: 1px solid rgb(243 244 246); }
:global(.dark) .bow-doc-editor :deep(th) { border-color: rgb(55 65 81); }
:global(.dark) .bow-doc-editor :deep(td) { border-color: rgb(31 41 55); }

/* Selected atom node outline */
.bow-doc-editor :deep(.ProseMirror-selectednode) { outline: 2px solid rgb(147 197 253); border-radius: 0.5rem; }
</style>

<!-- Print isolation: "Export as PDF" stamps `printing-doc-editor` on <html>,
     so only the editor content (charts included) prints — toolbar and app
     chrome are hidden, and the contenteditable caret/selection is suppressed. -->
<style>
@media print {
  html.printing-doc-editor body * { visibility: hidden !important; }
  html.printing-doc-editor .bow-doc-editor,
  html.printing-doc-editor .bow-doc-editor * { visibility: visible !important; }
  html.printing-doc-editor .bow-doc-editor {
    /* `absolute` (not `fixed`) so the document flows across pages: a fixed
       element is clipped to a single viewport box, cutting the PDF off after
       a couple of pages. No `bottom`/`inset` so height follows the content. */
    position: absolute !important;
    top: 0 !important;
    left: 0 !important;
    right: 0 !important;
    overflow: visible !important;
    height: auto !important;
    padding: 0 !important;
  }
  html.printing-doc-editor .bow-doc-editor .ProseMirror { caret-color: transparent !important; }
  html.printing-doc-editor .bow-doc-editor .ProseMirror-selectednode { outline: none !important; }
}
</style>
