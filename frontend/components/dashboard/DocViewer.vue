<template>
  <div class="doc-viewer h-full overflow-y-auto bg-white dark:bg-gray-900">
    <article class="bow-doc" :class="{ 'bow-doc--compact': compact }" :dir="docDir">
      <template v-for="(block, i) in blocks" :key="i">
        <!-- Markdown prose -->
        <div v-if="block.type === 'md'" class="bow-doc-md" v-html="block.html" />

        <!-- Live visualization -->
        <DocVizEmbed v-else-if="block.type === 'viz'" :viz="vizById(block.vizId)" />

        <!-- Mermaid diagram -->
        <DocMermaid v-else-if="block.type === 'mermaid'" :code="block.code" />

        <!-- Multi-column layout -->
        <div
          v-else-if="block.type === 'columns'"
          class="doc-columns my-4"
          :style="{ '--doc-cols': block.columns.length }"
        >
          <div v-for="(col, ci) in block.columns" :key="ci" class="min-w-0">
            <template v-for="(cb, cbi) in col" :key="cbi">
              <div v-if="cb.type === 'md'" class="bow-doc-md" v-html="cb.html" />
              <DocVizEmbed v-else-if="cb.type === 'viz'" :viz="vizById(cb.vizId)" />
              <DocMermaid v-else-if="cb.type === 'mermaid'" :code="cb.code" />
            </template>
          </div>
        </div>
      </template>
    </article>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js'
import { detectDocDir } from '~/utils/docDirection'
import DocVizEmbed from '~/components/dashboard/DocVizEmbed.vue'
import DocMermaid from '~/components/dashboard/DocMermaid.vue'

interface DocViz {
  id: string
  title?: string
  view?: any
  rows?: any[]
  columns?: any[]
  dataModel?: any
  stepStatus?: string
}

type DocBlock =
  | { type: 'md'; html: string }
  | { type: 'viz'; vizId: string }
  | { type: 'mermaid'; code: string }
  | { type: 'columns'; columns: DocBlock[][] }

const props = defineProps<{
  markdown: string
  visualizations?: DocViz[]
  compact?: boolean
}>()

const md = new MarkdownIt({
  html: false, // raw HTML in docs is never rendered — reliability + XSS posture
  linkify: true,
  typographer: true,
  breaks: false,
  highlight(str: string, lang: string): string {
    try {
      if (lang && hljs.getLanguage(lang)) {
        return hljs.highlight(str, { language: lang, ignoreIllegals: true }).value
      }
    } catch { /* fall through to escaped text */ }
    return ''
  },
})

const vizMap = computed(() => {
  const m = new Map<string, DocViz>()
  for (const v of props.visualizations || []) m.set(String(v.id).toLowerCase(), v)
  return m
})

function vizById(id: string): DocViz | null {
  return vizMap.value.get(id.toLowerCase()) || null
}

const VIZ_LINE_RE = /^\s*\{\{\s*viz:\s*([0-9a-fA-F-]{8,64})\s*\}\}\s*$/
const VIZ_INLINE_RE = /\{\{\s*viz:\s*([0-9a-fA-F-]{8,64})\s*\}\}/g
const FENCE_RE = /^\s*(```|~~~)\s*(\S*)/

function renderMd(buffer: string[], out: DocBlock[]) {
  const text = buffer.join('\n').trim()
  if (!text) return
  const html = DOMPurify.sanitize(md.render(text))
  if (html.trim()) out.push({ type: 'md', html })
}

/**
 * Parse doc markdown into renderable blocks.
 * - fence-aware: placeholders/columns markers inside ```/~~~ are literal text
 * - ```mermaid fences become mermaid blocks (rendered as diagrams)
 * - {{viz:<uuid>}} become viz blocks (inline occurrences split the paragraph)
 * - ::: columns / ::: col / ::: become a columns block (one level, no nesting)
 */
function parseBlocks(markdown: string, allowColumns = true): DocBlock[] {
  const out: DocBlock[] = []
  const lines = (markdown || '').split('\n')
  let buffer: string[] = []
  let i = 0

  while (i < lines.length) {
    const line = lines[i]
    const fence = line.match(FENCE_RE)

    if (fence) {
      const marker = fence[1]
      const lang = (fence[2] || '').toLowerCase()
      // Collect the fenced block verbatim
      const fenceLines: string[] = []
      i++
      let closed = false
      while (i < lines.length) {
        if (lines[i].trim().startsWith(marker)) { closed = true; i++; break }
        fenceLines.push(lines[i])
        i++
      }
      if (lang === 'mermaid') {
        renderMd(buffer, out); buffer = []
        out.push({ type: 'mermaid', code: fenceLines.join('\n') })
      } else {
        // Re-emit the fence for markdown-it to render (with highlight)
        buffer.push(`${marker}${fence[2] || ''}`, ...fenceLines, marker)
        if (!closed) buffer.push(marker) // guard unclosed fence
      }
      continue
    }

    // Columns container (top level only)
    if (allowColumns && /^:::\s*columns\s*$/.test(line.trim())) {
      renderMd(buffer, out); buffer = []
      const colChunks: string[][] = [[]]
      i++
      let depthClosed = false
      while (i < lines.length) {
        const l = lines[i]
        if (/^:::\s*col\s*$/.test(l.trim())) { colChunks.push([]); i++; continue }
        if (/^:::\s*$/.test(l.trim())) { depthClosed = true; i++; break }
        colChunks[colChunks.length - 1].push(l)
        i++
      }
      const columns = colChunks
        .map(chunk => parseBlocks(chunk.join('\n'), false))
        .filter(col => col.length > 0)
      if (columns.length > 1) {
        out.push({ type: 'columns', columns })
      } else if (columns.length === 1) {
        // Single column — flatten, no grid needed
        out.push(...columns[0])
      }
      if (!depthClosed && columns.length === 0) {
        // Malformed container with no content — ignore silently
      }
      continue
    }

    // Whole-line viz placeholder
    const lineViz = line.match(VIZ_LINE_RE)
    if (lineViz) {
      renderMd(buffer, out); buffer = []
      out.push({ type: 'viz', vizId: lineViz[1].toLowerCase() })
      i++
      continue
    }

    // Inline viz placeholder(s) inside a prose line: split around them
    if (VIZ_INLINE_RE.test(line)) {
      VIZ_INLINE_RE.lastIndex = 0
      let rest = line
      let m: RegExpExecArray | null
      while ((m = VIZ_INLINE_RE.exec(rest)) !== null) {
        const before = rest.slice(0, m.index)
        if (before.trim()) buffer.push(before)
        renderMd(buffer, out); buffer = []
        out.push({ type: 'viz', vizId: m[1].toLowerCase() })
        rest = rest.slice(m.index + m[0].length)
        VIZ_INLINE_RE.lastIndex = 0
      }
      if (rest.trim()) buffer.push(rest)
      i++
      continue
    }

    buffer.push(line)
    i++
  }

  renderMd(buffer, out)
  return out
}

const blocks = computed<DocBlock[]>(() => parseBlocks(props.markdown))

// Document direction inferred from content (Hebrew/Arabic → RTL). Charts,
// tables and lists use logical CSS properties, so they flip automatically.
const docDir = computed(() => detectDocDir(props.markdown))
</script>

<style scoped>
.bow-doc {
  max-width: 46rem;
  margin: 0 auto;
  padding: 3rem 1.5rem 5rem;
  font-size: 0.8125rem; /* 13px — compact document-scale body text */
  line-height: 1.65;
  color: rgb(55 65 81); /* gray-700 */
}
.bow-doc--compact { padding: 1.5rem 1rem 3rem; }
:global(.dark) .bow-doc { color: rgb(209 213 219); } /* gray-300 */

.doc-columns {
  display: grid;
  grid-template-columns: repeat(var(--doc-cols, 2), minmax(0, 1fr));
  gap: 1.5rem;
}
@media (max-width: 640px) {
  .doc-columns { grid-template-columns: 1fr; }
}

/* ---- Typography for rendered markdown (v-html → :deep) ---- */
.bow-doc-md :deep(h1) {
  font-size: 1.75rem;
  line-height: 1.25;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: rgb(17 24 39);
  margin: 0 0 1.25rem;
}
.bow-doc-md :deep(h1:not(:first-child)) { margin-top: 2.5rem; }
.bow-doc-md :deep(h2) {
  font-size: 1.25rem;
  line-height: 1.35;
  font-weight: 600;
  letter-spacing: -0.01em;
  color: rgb(17 24 39);
  margin: 2.25rem 0 0.75rem;
  padding-bottom: 0.375rem;
  border-bottom: 1px solid rgb(243 244 246);
}
.bow-doc-md :deep(h3) {
  font-size: 1.0625rem;
  font-weight: 600;
  color: rgb(31 41 55);
  margin: 1.75rem 0 0.5rem;
}
.bow-doc-md :deep(h4) {
  font-size: 0.9375rem;
  font-weight: 600;
  color: rgb(31 41 55);
  margin: 1.5rem 0 0.375rem;
}
:global(.dark) .bow-doc-md :deep(h1),
:global(.dark) .bow-doc-md :deep(h2) { color: rgb(243 244 246); border-color: rgb(31 41 55); }
:global(.dark) .bow-doc-md :deep(h3),
:global(.dark) .bow-doc-md :deep(h4) { color: rgb(229 231 235); }

.bow-doc-md :deep(p) { margin: 0.75rem 0; }
.bow-doc-md :deep(a) { color: rgb(37 99 235); text-decoration: none; border-bottom: 1px solid rgb(191 219 254); }
.bow-doc-md :deep(a:hover) { border-color: rgb(37 99 235); }
:global(.dark) .bow-doc-md :deep(a) { color: rgb(96 165 250); border-color: rgb(30 58 138); }

.bow-doc-md :deep(strong) { font-weight: 600; color: rgb(17 24 39); }
:global(.dark) .bow-doc-md :deep(strong) { color: rgb(243 244 246); }

.bow-doc-md :deep(ul), .bow-doc-md :deep(ol) { margin: 0.75rem 0; padding-inline-start: 1.5rem; }
.bow-doc-md :deep(ul) { list-style: disc; }
.bow-doc-md :deep(ol) { list-style: decimal; }
.bow-doc-md :deep(li) { margin: 0.25rem 0; }
.bow-doc-md :deep(li > ul), .bow-doc-md :deep(li > ol) { margin: 0.25rem 0; }

.bow-doc-md :deep(blockquote) {
  margin: 1.25rem 0;
  padding: 0.25rem 0 0.25rem 1rem;
  border-inline-start: 3px solid rgb(229 231 235);
  color: rgb(107 114 128);
  font-style: italic;
}
:global(.dark) .bow-doc-md :deep(blockquote) { border-color: rgb(55 65 81); color: rgb(156 163 175); }

.bow-doc-md :deep(hr) { margin: 2.5rem auto; border: 0; border-top: 1px solid rgb(229 231 235); width: 100%; }
:global(.dark) .bow-doc-md :deep(hr) { border-color: rgb(55 65 81); }

/* Inline code */
.bow-doc-md :deep(code) {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.8125rem;
  background: rgb(243 244 246);
  color: rgb(31 41 55);
  border-radius: 0.25rem;
  padding: 0.125rem 0.375rem;
}
:global(.dark) .bow-doc-md :deep(code) { background: rgb(31 41 55); color: rgb(229 231 235); }

/* Code blocks */
.bow-doc-md :deep(pre) {
  margin: 1.25rem 0;
  padding: 1rem;
  background: rgb(249 250 251);
  border: 1px solid rgb(243 244 246);
  border-radius: 0.5rem;
  overflow-x: auto;
}
.bow-doc-md :deep(pre code) { background: transparent; padding: 0; font-size: 0.8125rem; line-height: 1.6; }
:global(.dark) .bow-doc-md :deep(pre) { background: rgb(17 24 39); border-color: rgb(31 41 55); }

/* Tables */
.bow-doc-md :deep(table) {
  width: 100%;
  margin: 1.25rem 0;
  border-collapse: collapse;
  font-size: 0.875rem;
}
.bow-doc-md :deep(th) {
  text-align: start;
  font-weight: 600;
  color: rgb(55 65 81);
  padding: 0.5rem 0.75rem;
  border-bottom: 2px solid rgb(229 231 235);
  white-space: nowrap;
}
.bow-doc-md :deep(td) {
  padding: 0.5rem 0.75rem;
  border-bottom: 1px solid rgb(243 244 246);
  vertical-align: top;
}
.bow-doc-md :deep(tr:last-child td) { border-bottom: none; }
:global(.dark) .bow-doc-md :deep(th) { color: rgb(209 213 219); border-color: rgb(55 65 81); }
:global(.dark) .bow-doc-md :deep(td) { border-color: rgb(31 41 55); }

/* Print: clean page for PDF export */
@media print {
  .doc-viewer { overflow: visible !important; }
  .bow-doc { padding: 0; max-width: 100%; }
}

</style>

<!-- Print isolation: when ArtifactFrame's Print button stamps `printing-doc` on
     <html>, only the document prints — app chrome, chat and toolbars vanish. -->
<style>
@media print {
  html.printing-doc body * { visibility: hidden !important; }
  html.printing-doc .doc-viewer,
  html.printing-doc .doc-viewer * { visibility: visible !important; }
  html.printing-doc .doc-viewer {
    /* `absolute` (not `fixed`) so the document flows across pages: a fixed
       element is clipped to a single viewport box, cutting the PDF off after
       a couple of pages. No `bottom`/`inset` so height follows the content. */
    position: absolute !important;
    top: 0 !important;
    left: 0 !important;
    right: 0 !important;
    overflow: visible !important;
    height: auto !important;
  }
}
</style>
