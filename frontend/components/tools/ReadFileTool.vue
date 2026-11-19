<template>
  <div class="min-w-0">
    <!-- The same header geometry in every state and in grouped activity. -->
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-book-open" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>{{ modelTitle || t(isReading ? 'tools.readFile.reading' : 'tools.readFile.read', { name: fileLabel }) }}</template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">


    <!-- Document previews stay inside the tool disclosure. -->
    <button
      v-if="derivedGalleryCollapsed"
      type="button"
      class="mb-2 text-chat-2 px-2 py-1 rounded border border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
      @click="galleryOpen = true"
    >
      <Icon name="heroicons-photo" class="w-3 h-3 inline align-text-bottom me-1" />
      {{ t('tools.readFile.showPagePreviews', { n: (preview.image_file_ids || []).length }) }}
    </button>
    <Transition name="fade" appear>
      <div v-if="showVisualPreview && toolHeaderExpanded" class="mb-2 w-full min-w-0">
        <!-- Inline frame stays compact; the side panel is the full-size view. -->
        <FilePreview
          v-if="preview.kind === 'pdf' || preview.kind === 'image'"
          :kind="preview.kind"
          :file-id="preview.file_id"
          :image-file-ids="preview.image_file_ids"
          :target-page="preview.target_page"
          :pages-total="preview.pages_total"
          :truncated="preview.truncated"
          :name="rj.file_name"
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
      <div v-if="toolHeaderExpanded && expandable" class="mt-2 w-full min-w-0 text-chat-2 text-ink-2">
        <div v-if="hasError" class="mb-2 break-words text-chat-3 text-ink-muted" data-testid="read-file-error">{{ errorMessage || t('tools.common.failed') }}</div>
        <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted"><DataSourceIcon v-if="connIcon" :type="connIcon.type" :connector-key="connIcon.connectorKey" class="w-3.5 h-3.5 shrink-0" /><span v-if="contentType" class="px-1 py-0.5 rounded bg-surface-muted">{{ contentType }}</span><span v-if="rowCount != null">{{ t('tools.readFile.rowsCols', { rows: rowCount, cols: colCount }) }}</span><span v-if="truncated" class="text-yellow-600">{{ t('tools.readFile.truncated') }}</span><span v-if="windowed">{{ windowLabel }}</span><span v-if="pagesShown">{{ pagesTotal
            ? t('tools.readFile.pagesOfTotal', { shown: pagesShown, total: pagesTotal })
            : t('tools.readFile.pages', { shown: pagesShown }) }}</span></div>

        <div v-if="filePath" class="mb-1 text-chat-3 text-gray-500 dark:text-gray-400">
          <span class="text-ink-muted">{{ t('tools.readFile.pathLabel') }}</span>
          <code class="ms-1 px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 break-all" dir="ltr">{{ filePath }}</code>
        </div>
        <!-- The document/picture IS the content — don't repeat it as a text dump. -->
        <pre v-if="hasContent && !showVisualPreview" class="text-chat-2 bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded p-2 max-h-64 overflow-auto whitespace-pre-wrap">{{ previewText }}</pre>
        <div v-if="sessionFileId" class="mt-2 text-chat-2 text-gray-500 dark:text-gray-400">
          <Icon name="heroicons-paper-clip" class="w-3 h-3 inline align-text-bottom me-0.5" />
          {{ t('tools.readFile.attachedAsSessionFile') }}
          <code class="ms-1 px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400">{{ sessionFileId.slice(0, 8) }}…</code>
        </div>
        <ToolCallParams :params="toolExecution?.arguments_json" />
      </div>
    </Transition>

  
    </div>
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)

const { t } = useResponseI18n()

import { computed, ref } from 'vue'
import { toolErrorText } from '~/utils/toolError'
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
  error_message?: string
  result_summary?: string
  result_json?: any
  arguments_json?: any
}

const props = withDefaults(defineProps<{
  toolExecution: ToolExecution
  initiallyCollapsed?: boolean
  dataSources?: any[]
  /** Host page has a side panel to open documents into. */
  canExpand?: boolean
  /** Anonymous/share view (`/c/[token]` passes true). PDF and image previews
   *  authorize via `/embed_token` / `/content`, which require a signed-in
   *  user — for guests they 401 and the card would show a permanent
   *  "Preview unavailable" box. The inline CSV table carries its own data,
   *  so it stays. */
  readonly?: boolean
}>(), { canExpand: false, readonly: false })

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
const isReading = computed(() => ['running', 'pending', 'queued', 'in_progress'].includes(status.value))

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
const errorMessage = computed(() => toolErrorText(rj.value.error || props.toolExecution?.error_message))
const hasError = computed(() => !isReading.value && (!!errorMessage.value || ['error', 'failed'].includes(status.value)))

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
const expandable = computed(() => hasError.value || hasContent.value || hasParams.value)
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

// Document page renders (docx/pptx rasters and the like) auto-collapse; the
// user opens them explicitly. Absent flag = older execution = old behavior.
const galleryOpen = ref(false)
const isDerivedGallery = computed(
  () => preview.value.kind === 'image' && preview.value.derived === true,
)
const derivedGalleryCollapsed = computed(() =>
  isDerivedGallery.value
  && !galleryOpen.value
  && !props.readonly
  && status.value !== 'running'
  && !errorMessage.value,
)

const showVisualPreview = computed(() => {
  if (isReading.value || hasError.value) return false
  // Token-backed viewers need an authenticated user; guests get the
  // text-only card they had before previews existed.
  if (props.readonly) return preview.value.kind === 'table'
  if (isDerivedGallery.value && !galleryOpen.value) return false
  return ['pdf', 'image', 'table'].includes(preview.value.kind)
})

const imageModal = ref<any>(null)
function openImage(fileId: string) {
  imageModal.value?.open({ id: fileId, filename: rj.value.file_name || '' })
}

</script>

<style scoped>
.fade-enter-active, .fade-leave-active { transition: opacity 0.25s ease, transform 0.25s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; transform: translateY(2px); }
</style>
