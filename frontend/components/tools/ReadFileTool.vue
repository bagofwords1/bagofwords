<template>
  <div class="mt-1">
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-xs text-gray-500 dark:text-gray-400">
        <span v-if="status === 'running'" class="flex items-center">
          <Spinner class="w-3 h-3 me-1.5 shrink-0 text-gray-400" />
          <span class="tool-shimmer">{{ modelTitle ? modelTitle + '…' : $t('tools.readFile.reading', { name: fileLabel }) }}</span>
        </span>
        <span
          v-else
          class="text-gray-700 dark:text-gray-300 flex items-center"
          :class="expandable ? 'cursor-pointer' : ''"
          @click="expandable && (expanded = !expanded)"
          :aria-expanded="expandable ? expanded : undefined"
        >
          <Icon v-if="expandable" :name="expanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 me-1 text-gray-400 dark:text-gray-500 rtl-flip" />
          <DataSourceIcon v-if="connIcon" :type="connIcon.type" :connector-key="connIcon.connectorKey" class="w-3 h-3 me-1 shrink-0" />
          <Icon v-else name="heroicons-document-arrow-down" class="w-3 h-3 me-1 text-gray-400" />
          <span>{{ modelTitle || $t('tools.readFile.read', { name: fileLabel }) }}</span>
          <span v-if="contentType" class="ms-2 text-[10px] px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400">{{ contentType }}</span>
          <span v-if="rowCount != null" class="ms-2 text-gray-400">{{ $t('tools.readFile.rowsCols', { rows: rowCount, cols: colCount }) }}</span>
          <span v-if="truncated" class="ms-2 text-[10px] text-yellow-600">{{ $t('tools.readFile.truncated') }}</span>
          <span v-if="windowed" class="ms-2 text-[10px] px-1 py-0.5 rounded bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400">{{ windowLabel }}</span>
          <span v-if="pagesShown" class="ms-2 text-[10px] px-1 py-0.5 rounded bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400">{{ pagesTotal
            ? $t('tools.readFile.pagesOfTotal', { shown: pagesShown, total: pagesTotal })
            : $t('tools.readFile.pages', { shown: pagesShown }) }}</span>
        </span>
      </div>
    </Transition>

    <!-- Visual previews render on success without being asked, the way a
         chart does in create_data; only plain text waits behind the chevron. -->
    <Transition name="fade" appear>
      <div v-if="showVisualPreview" class="mb-2">
        <FilePreview
          v-if="preview.kind === 'pdf' || preview.kind === 'image'"
          :kind="preview.kind"
          :file-id="preview.file_id"
          :image-file-ids="preview.image_file_ids"
          :target-page="preview.target_page"
          :pages-total="preview.pages_total"
          :truncated="preview.truncated"
          :name="rj.file_name"
          :expanded="expanded"
          :can-expand="canExpand"
          @open="openImage"
          @expand="emitOpenPanel"
        />
        <FileTablePreview
          v-else-if="preview.kind === 'table'"
          :csv="rj.csv"
          :row-count="rj.row_count"
          :col-count="rj.col_count"
        />
      </div>
    </Transition>

    <ImagePreviewModal ref="imageModal" />

    <Transition name="fade" appear>
      <div v-if="expanded && expandable" class="text-xs text-gray-600 dark:text-gray-400">
        <div v-if="filePath" class="mb-1 text-[11px] text-gray-500 dark:text-gray-400">
          <span class="text-gray-400 dark:text-gray-500">{{ $t('tools.readFile.pathLabel') }}</span>
          <code class="ms-1 px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 break-all" dir="ltr">{{ filePath }}</code>
        </div>
        <pre v-if="hasContent && preview.kind !== 'table'" class="text-[11px] bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded p-2 max-h-64 overflow-auto whitespace-pre-wrap">{{ previewText }}</pre>
        <div v-if="sessionFileId" class="mt-2 text-[11px] text-gray-500 dark:text-gray-400">
          <Icon name="heroicons-paper-clip" class="w-3 h-3 inline align-text-bottom me-0.5" />
          {{ $t('tools.readFile.attachedAsSessionFile') }}
          <code class="ms-1 px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400">{{ sessionFileId.slice(0, 8) }}…</code>
        </div>
        <ToolCallParams :params="toolExecution?.arguments_json" />
      </div>
    </Transition>

    <div v-if="status !== 'running' && !hasContent && errorMessage" class="text-xs text-amber-600 mt-1">{{ errorMessage }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import Spinner from '~/components/Spinner.vue'
import ToolCallParams from '~/components/tools/ToolCallParams.vue'
import FilePreview from '~/components/FilePreview.vue'
import FileTablePreview from '~/components/FileTablePreview.vue'
import ImagePreviewModal from '~/components/ImagePreviewModal.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import { useToolConnectionIcon, FILE_SOURCE_TYPES, fileToolNoun } from '~/composables/useToolConnectionIcon'

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_summary?: string
  result_json?: any
  arguments_json?: any
}

const props = withDefaults(defineProps<{
  toolExecution: ToolExecution
  dataSources?: any[]
  /** Host page has a side panel to open documents into. */
  canExpand?: boolean
}>(), { canExpand: false })

// Only documents and images earn the panel — a ten-row table is already fully
// readable in the card, and half a screen of it is just noise.
const emit = defineEmits<{ (e: 'openFilePreview', payload: any): void }>()
function emitOpenPanel() {
  emit('openFilePreview', {
    fileId: preview.value.file_id,
    kind: preview.value.kind,
    targetPage: preview.value.target_page,
    pagesTotal: preview.value.pages_total,
    imageFileIds: preview.value.image_file_ids,
    name: rj.value.file_name || '',
  })
}
// files / emails / pages — same card, source-appropriate noun.
const noun = computed(() => fileToolNoun(props.toolExecution?.tool_name))

const connIcon = useToolConnectionIcon(
  () => props.toolExecution,
  () => props.dataSources,
  { connectionTypes: FILE_SOURCE_TYPES },
)

const status = computed(() => props.toolExecution?.status || '')

const modelTitle = computed<string>(() => {
  const t = props.toolExecution?.arguments_json?.title
  return typeof t === 'string' && t.trim() ? t.trim() : ''
})
const rj = computed<any>(() => props.toolExecution?.result_json || {})

const fileLabel = computed(() => {
  if (rj.value.file_name) return rj.value.file_name
  const fid = props.toolExecution?.arguments_json?.file_id
  if (typeof fid === 'string' && fid) {
    // Path-shaped ids (network_dir / s3) carry a readable name; opaque
    // provider ids (Graph) fall back to a short prefix.
    const leaf = fid.split('/').pop() || fid
    if (fid.includes('/') || leaf.includes('.')) return leaf
    return fid.slice(0, 8)
  }
  return noun.value.one
})
// Page-range (document) reads: show which pages of how many.
const pagesShown = computed(() => rj.value.pages_shown || '')
const pagesTotal = computed(() => rj.value.pages_total)
// Human-readable location shown in the expanded view: backend-provided path
// (source-relative for connectors, upload name for attachments), falling back
// to a path-shaped file_id from the call args.
const filePath = computed(() => {
  if (rj.value.path) return rj.value.path
  const fid = props.toolExecution?.arguments_json?.file_id
  if (typeof fid === 'string' && (fid.includes('/') || (fid.split('/').pop() || '').includes('.'))) return fid
  return ''
})
const contentType = computed(() => rj.value.content_type || '')
const rowCount = computed(() => rj.value.row_count)
const colCount = computed(() => rj.value.col_count)
const truncated = computed(() => !!rj.value.truncated)
const sessionFileId = computed(() => rj.value.session_file_id || '')
const errorMessage = computed(() => rj.value.error || '')

// Windowed (byte-range) read state — offset comes from the call args, the
// cursor from the result. Shown as a badge so paging progress is visible.
const windowed = computed(() => !!rj.value.windowed)
const windowLabel = computed(() => {
  if (!windowed.value) return ''
  const offset = props.toolExecution?.arguments_json?.offset ?? 0
  const total = rj.value.total_size
  const pos = total ? `${offset}–${rj.value.next_cursor ?? total} / ${total}` : `offset ${offset}`
  return rj.value.eof ? `window ${pos} · eof` : `window ${pos}`
})

const hasContent = computed(() => !!(rj.value.csv || rj.value.text || rj.value.byte_count))
// Params stay nested behind the expand toggle even when there's no preview
// (binary / error read) — raw ids like connection_id must not show collapsed.
const hasParams = computed(() => {
  const src = props.toolExecution?.arguments_json
  if (!src || typeof src !== 'object') return false
  return Object.entries(src).some(([k, v]) => k !== 'title' && v !== null && v !== undefined && v !== '')
})
const expandable = computed(() => hasContent.value || hasParams.value)
const previewText = computed(() => {
  if (rj.value.csv) return String(rj.value.csv).slice(0, 4000)
  if (rj.value.text) return String(rj.value.text).slice(0, 4000)
  if (rj.value.byte_count) return `(binary, ${rj.value.byte_count} bytes)`
  return ''
})

// The server decides what to render — the stored session file is often a
// derivative of the source (report.pdf -> report.pdf.txt), so the file name
// and content_type here cannot answer it. Older tool executions predate the
// contract and simply get no visual preview.
const preview = computed<any>(() => rj.value.preview || { kind: 'none' })
const showVisualPreview = computed(() =>
  status.value !== 'running'
  && !errorMessage.value
  && ['pdf', 'image', 'table'].includes(preview.value.kind),
)

const imageModal = ref<any>(null)
function openImage(fileId: string) {
  imageModal.value?.open({ id: fileId, filename: rj.value.file_name || '' })
}

const expanded = ref(false)
</script>

<style scoped>
.tool-shimmer {
  background: linear-gradient(90deg, #888 0%, #999 25%, #ccc 50%, #999 75%, #888 100%);
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: shimmer 2s linear infinite;
}
@keyframes shimmer { 0% { background-position: -100% 0; } 100% { background-position: 100% 0; } }
.fade-enter-active, .fade-leave-active { transition: opacity 0.25s ease, transform 0.25s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; transform: translateY(2px); }
</style>
