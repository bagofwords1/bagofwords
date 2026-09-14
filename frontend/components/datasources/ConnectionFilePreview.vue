<template>
  <div>
    <div v-if="unavailableReason" class="rounded-md border border-dashed border-gray-200 dark:border-gray-700 px-4 py-8 text-center">
      <UIcon :name="fileIcon(file.mime_type, name)" class="w-8 h-8 mx-auto text-gray-300 dark:text-gray-600" />
      <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">{{ unavailableReason }}</p>
      <a v-if="webUrl" :href="webUrl" target="_blank" rel="noopener noreferrer"
         class="mt-2 inline-flex items-center gap-1 text-xs text-blue-600 dark:text-blue-400 hover:underline">
        {{ $t('agentsPage.fileBrowserOpenInSource') }}<UIcon name="i-heroicons-arrow-top-right-on-square" class="w-3.5 h-3.5" />
      </a>
    </div>
    <div v-else-if="loading" class="text-xs text-gray-400 dark:text-gray-500 py-6 flex items-center justify-center gap-1.5">
      <UIcon name="i-heroicons-arrow-path" class="w-3.5 h-3.5 animate-spin" />{{ $t('agentsPage.fileBrowserPreviewLoading') }}
    </div>
    <div v-else-if="error !== null" class="text-xs text-red-700 dark:text-red-300 bg-red-50 dark:bg-red-900/20 rounded px-2 py-1.5 flex items-start justify-between gap-2">
      <span class="min-w-0 break-words">{{ $t('agentsPage.fileBrowserPreviewError') }} {{ error }}</span>
      <button type="button" class="shrink-0 underline hover:no-underline" @click="load(requestedSheet)">{{ $t('agentsPage.fileBrowserRetry') }}</button>
    </div>
    <template v-else>
      <!-- The same viewer read_file's cards use; `expanded` is its full-height
           reader mode, so it needs a sized box to fill. -->
      <div v-if="url" class="h-[65vh]">
        <FilePreview :kind="kind === 'image' ? 'image' : 'pdf'" :src="url" :name="name" expanded />
      </div>
      <template v-else-if="kind === 'table' && table">
        <div v-if="table.sheets.length > 1" class="flex items-center gap-1 mb-2 overflow-x-auto">
          <button v-for="s in table.sheets" :key="s" type="button"
                  class="shrink-0 px-2 py-0.5 rounded text-[11px] border"
                  :class="s === table.sheet
                    ? 'border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300'
                    : 'border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800'"
                  @click="load(s)">{{ s }}</button>
        </div>
        <div class="max-h-[65vh] overflow-auto">
          <FileTablePreview :csv="table.csv" :row-count="table.row_count" :col-count="table.col_count" :max-rows="200" :max-cols="30" />
        </div>
      </template>
      <template v-else-if="kind === 'text' && text !== null">
        <pre dir="auto" class="max-h-[65vh] overflow-auto whitespace-pre-wrap break-words text-[11px] font-mono text-gray-800 dark:text-gray-200 bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md p-3">{{ text }}</pre>
        <div v-if="textTruncated" class="mt-1 text-[10px] text-gray-400 dark:text-gray-500">
          {{ $t('agentsPage.fileBrowserPreviewTextTruncated', { n: MAX_TEXT_CHARS.toLocaleString() }) }}
        </div>
      </template>
    </template>
  </div>
</template>

<script setup lang="ts">
import FilePreview from '~/components/FilePreview.vue'
import FileTablePreview from '~/components/FileTablePreview.vue'
import {
  IMAGE_MIME_BY_EXT, fileExt, fileIcon, fileName, formatBytes, isAnimatedImage, previewKind, type BrowseFile,
} from '~/utils/fileTree'

const props = defineProps<{ dsId: string; connectionId: string; file: BrowseFile }>()
// Content is on screen and has its full height — the host scrolls it into view then.
const emit = defineEmits<{ (e: 'ready'): void }>()

// Mirrors the server's _PREVIEW_MAX_BYTES: past it the request would only 413,
// after the server had already downloaded the whole file from the source.
const MAX_PREVIEW_BYTES = 25 * 1024 * 1024
const MAX_TEXT_CHARS = 200_000

const name = computed(() => fileName(props.file))
const kind = computed(() => previewKind(name.value))
const tooLarge = computed(() => (props.file.size ?? 0) > MAX_PREVIEW_BYTES)
const webUrl = computed(() => (props.file.web_url && /^https?:\/\//i.test(props.file.web_url) ? props.file.web_url : null))

const { t } = useI18n()
const loading = ref(false)
const error = ref<string | null>(null)
// Set when the server says this file can't be previewed here (no LibreOffice,
// oversize) — retrying won't change that, so it gets the neutral box, not an error.
const serverUnavailable = ref<string | null>(null)
const unavailableReason = computed(() => {
  if (tooLarge.value) return t('agentsPage.fileBrowserPreviewTooLarge', { size: formatBytes(props.file.size) })
  if (kind.value === 'none') return t('agentsPage.fileBrowserPreviewUnsupported')
  return serverUnavailable.value
})
const url = ref<string | null>(null)
const text = ref<string | null>(null)
const textTruncated = ref(false)
const table = ref<{ csv: string; row_count: number; col_count: number; sheets: string[]; sheet: string | null } | null>(null)

const endpoint = (params: Record<string, string>) =>
  `/data_sources/${props.dsId}/connections/${props.connectionId}/files/content?` +
  new URLSearchParams({ file_id: props.file.id, ...params }).toString()

// A blob request's error body arrives as a Blob too.
async function showError(err: any) {
  let d = err?.data
  if (d instanceof Blob) { try { d = JSON.parse(await d.text()) } catch { d = null } }
  const detail = typeof d?.detail === 'string' ? d.detail : ''
  const status = err?.statusCode ?? err?.status
  if (status === 422 || status === 413) serverUnavailable.value = detail || t('agentsPage.fileBrowserPreviewUnsupported')
  else error.value = detail
}

function clear() {
  if (url.value) URL.revokeObjectURL(url.value)
  url.value = null
  text.value = null
  textTruncated.value = false
  error.value = null
  serverUnavailable.value = null
}

// The first frame of an animated image, as a still PNG — what SharePoint's own
// viewer shows. createImageBitmap takes only the first (default) frame of an
// animation, per the HTML spec. On any failure keep the original.
async function firstFrame(blob: Blob): Promise<Blob> {
  if (typeof createImageBitmap !== 'function') return blob
  try {
    const bitmap = await createImageBitmap(blob)
    const canvas = document.createElement('canvas')
    canvas.width = bitmap.width
    canvas.height = bitmap.height
    canvas.getContext('2d')?.drawImage(bitmap, 0, 0)
    bitmap.close()
    return (await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/png'))) || blob
  } catch {
    return blob
  }
}

let seq = 0
// The sheet last ASKED for, not the one on screen: after a failed switch from
// A to B, "Try again" must retry B, while `table.sheet` still says A.
const requestedSheet = ref<string | null>(null)
async function load(sheet?: string | null) {
  const k = kind.value
  const mine = ++seq
  requestedSheet.value = sheet ?? null
  clear()
  if (tooLarge.value || k === 'none') return
  loading.value = true
  try {
    if (k === 'table') {
      const res = await useMyFetch(endpoint({ format: 'table', ...(sheet ? { sheet } : {}) }), { method: 'GET' })
      if (mine !== seq) return
      if (res.error.value) { await showError(res.error.value); return }
      table.value = res.data.value as any
      return
    }
    const res = await useMyFetch(endpoint(k === 'office' ? { format: 'pdf' } : {}), { method: 'GET', responseType: 'blob' as any })
    if (mine !== seq) return
    if (res.error.value) { await showError(res.error.value); return }
    const blob = res.data.value as Blob
    if (k === 'text') {
      const s = await blob.text()
      text.value = s.slice(0, MAX_TEXT_CHARS)
      textTruncated.value = s.length > MAX_TEXT_CHARS
      return
    }
    // Type the blob ourselves from the extension: an iframe only ever gets a
    // PDF and an <img> only an image, whatever the source claimed.
    const type = k === 'image' ? (IMAGE_MIME_BY_EXT[fileExt(name.value)] || 'application/octet-stream') : 'application/pdf'
    let typed = new Blob([blob], { type })
    if (k === 'image' && isAnimatedImage(new Uint8Array(await blob.arrayBuffer()))) {
      typed = await firstFrame(typed)
      if (mine !== seq) return
    }
    url.value = URL.createObjectURL(typed)
  } catch (e: any) {
    if (mine === seq) error.value = e?.message || ''
  } finally {
    if (mine === seq) {
      loading.value = false
      nextTick(() => emit('ready'))
    }
  }
}

watch(() => props.file.id, () => { table.value = null; load() }, { immediate: true })
// Bump seq so a response still in flight is dropped instead of minting a blob URL nobody revokes.
onBeforeUnmount(() => { seq++; clear() })
</script>
