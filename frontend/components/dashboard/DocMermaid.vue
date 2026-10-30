<template>
  <div class="doc-mermaid my-6">
    <!-- Successful diagram -->
    <div v-if="svg && !failed" class="group relative">
      <div
        class="flex justify-center overflow-x-auto rounded-lg border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900 p-4"
        v-html="svg"
      />
      <!-- .stop/.prevent: in the doc editor a click on the diagram opens its
           source; the download button must not. -->
      <button
        type="button"
        contenteditable="false"
        class="doc-mermaid-download print:hidden absolute top-2 end-2 inline-flex items-center gap-1 rounded-md border border-gray-200 dark:border-gray-700 bg-white/90 dark:bg-gray-800/90 px-2 py-1 text-xs text-gray-600 dark:text-gray-300 shadow-sm opacity-0 transition-opacity group-hover:opacity-100 focus:opacity-100 hover:bg-gray-50 dark:hover:bg-gray-700 disabled:cursor-wait"
        :title="$t('docViewer.diagramDownloadPng')"
        :aria-label="$t('docViewer.diagramDownloadPng')"
        :disabled="exporting"
        @mousedown.stop.prevent
        @click.stop.prevent="downloadPng"
      >
        <Icon name="heroicons:arrow-down-tray" class="w-3.5 h-3.5" />
        <span>PNG</span>
      </button>
    </div>
    <!-- Loading -->
    <div v-else-if="!failed" class="flex items-center justify-center py-8">
      <Spinner class="w-4 h-4 text-gray-300" />
    </div>
    <!-- Fallback: show source, never a broken page -->
    <div v-else class="rounded-lg border border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-gray-900 p-4">
      <div class="mb-1 text-2xs uppercase tracking-wide text-ink-muted">{{ $t('docViewer.diagramFailed') }}</div>
      <pre class="text-xs text-gray-500 dark:text-gray-400 overflow-x-auto">{{ code }}</pre>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'
import Spinner from '~/components/Spinner.vue'
import { repairMermaid } from '~/utils/mermaidRepair'

const { t } = useI18n()

// keepLastGood: while the code is being live-edited (doc editor preview), a
// parse failure keeps the last successful diagram instead of flashing the
// source fallback on every intermediate keystroke.
const props = defineProps<{ code: string; keepLastGood?: boolean }>()

const svg = ref<string>('')
const failed = ref(false)
const exporting = ref(false)
let seq = 0
// The source that actually rendered (original or repaired) — the export
// re-renders exactly this.
let renderedCode = ''

async function render() {
  if (!props.keepLastGood) {
    failed.value = false
    svg.value = ''
  }
  const myTicket = ++seq

  let mermaid: any
  try {
    mermaid = (await import('mermaid')).default
    const isDark = document.documentElement.classList.contains('dark')
    mermaid.initialize({
      startOnLoad: false,
      // Without this, Mermaid appends its "Syntax error" bomb SVG to <body> on
      // every failed parse and never removes it; we show our own fallback.
      suppressErrorRendering: true,
      securityLevel: 'strict',
      theme: isDark ? 'dark' : 'neutral',
      fontFamily: 'ui-sans-serif, system-ui, sans-serif',
    })
  } catch {
    if (myTicket === seq && !(props.keepLastGood && svg.value)) failed.value = true
    return
  }

  // Fresh id per attempt: on a parse failure Mermaid (strict) injects an error
  // node keyed by id, so reusing one across attempts would collide.
  const renderOnce = async (code: string): Promise<string> => {
    const id = `doc-mermaid-${Math.random().toString(36).slice(2)}`
    const { svg: rendered } = await mermaid.render(id, code)
    return rendered
  }

  try {
    const rendered = await renderOnce(props.code)
    if (myTicket === seq) { svg.value = rendered; failed.value = false; renderedCode = props.code }
    return
  } catch {
    // Rescue: the most common LLM mistake is unquoted punctuation in a node
    // label (e.g. `SUM(Invoice.Total)`), which aborts the whole parse. Quote
    // the labels and retry once before showing the source fallback.
    const repaired = repairMermaid(props.code)
    if (repaired !== props.code) {
      try {
        const rendered = await renderOnce(repaired)
        if (myTicket === seq) { svg.value = rendered; failed.value = false; renderedCode = repaired }
        return
      } catch { /* fall through to source fallback */ }
    }
    if (myTicket === seq && !(props.keepLastGood && svg.value)) failed.value = true
  }
}

// PNG export. Rendered separately from the on-screen diagram because:
//  - Mermaid's default HTML labels live in <foreignObject>, and drawing such an
//    SVG onto a canvas taints it (toBlob throws SecurityError) — so labels are
//    rendered as plain SVG text here;
//  - the export always uses the light theme on white, whatever the app theme.
// The global config is restored afterwards so on-screen renders are unchanged.
async function downloadPng() {
  if (exporting.value || !renderedCode) return
  exporting.value = true
  const mermaid = (await import('mermaid')).default
  const isDark = document.documentElement.classList.contains('dark')
  const base = {
    startOnLoad: false,
    suppressErrorRendering: true,
    securityLevel: 'strict' as const,
    fontFamily: 'ui-sans-serif, system-ui, sans-serif',
  }
  try {
    mermaid.initialize({ ...base, theme: 'neutral', htmlLabels: false, flowchart: { htmlLabels: false } })
    const id = `doc-mermaid-export-${Math.random().toString(36).slice(2)}`
    const { svg: exportSvg } = await mermaid.render(id, renderedCode)
    const blob = await svgToPngBlob(exportSvg, 2)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'diagram.png'
    document.body.appendChild(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch {
    useToast().add({ title: t('docViewer.diagramExportFailed'), color: 'red' })
  } finally {
    mermaid.initialize({ ...base, theme: isDark ? 'dark' : 'neutral', htmlLabels: true, flowchart: { htmlLabels: true } })
    exporting.value = false
  }
}

async function svgToPngBlob(svgText: string, scale: number): Promise<Blob> {
  const doc = new DOMParser().parseFromString(svgText, 'image/svg+xml')
  const el = doc.documentElement
  // Mermaid emits width="100%" + max-width style; size from the viewBox.
  const vb = (el.getAttribute('viewBox') || '').split(/[\s,]+/).map(Number)
  const width = vb.length === 4 && vb[2] > 0 ? vb[2] : 800
  const height = vb.length === 4 && vb[3] > 0 ? vb[3] : 600
  el.setAttribute('width', String(width))
  el.setAttribute('height', String(height))
  el.removeAttribute('style')
  const xml = new XMLSerializer().serializeToString(el)
  const img = new Image()
  img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(xml)}`
  await img.decode()
  const pad = 16
  const canvas = document.createElement('canvas')
  canvas.width = Math.ceil((width + pad * 2) * scale)
  canvas.height = Math.ceil((height + pad * 2) * scale)
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('no 2d context')
  ctx.fillStyle = '#ffffff'
  ctx.fillRect(0, 0, canvas.width, canvas.height)
  ctx.scale(scale, scale)
  ctx.drawImage(img, pad, pad, width, height)
  const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/png'))
  if (!blob) throw new Error('toBlob returned null')
  return blob
}

onMounted(render)
watch(() => props.code, render)
</script>

<style scoped>
.doc-mermaid :deep(svg) { max-width: 100%; height: auto; }
</style>
