<template>
  <div>
    <div v-if="connectRequired" class="text-xs text-amber-700 dark:text-amber-300 bg-amber-50 dark:bg-amber-900/20 rounded px-2 py-1.5">
      {{ $t('agentsPage.fileBrowserConnectRequired') }}
    </div>
    <template v-else>
      <div ref="toolbarEl" class="flex items-center gap-2 mb-2">
        <nav class="flex items-center gap-0.5 min-w-0 flex-1 text-xs overflow-x-auto">
          <button type="button" class="shrink-0 px-1 py-0.5 rounded hover:bg-gray-100 dark:hover:bg-gray-800"
                  :class="segments.length ? 'text-gray-500 dark:text-gray-400' : 'font-medium text-gray-800 dark:text-gray-200'"
                  @click="goTo([])">{{ $t('agentsPage.fileBrowserRoot') }}</button>
          <template v-for="(seg, i) in segments" :key="i">
            <UIcon name="i-heroicons-chevron-right" class="w-3 h-3 shrink-0 text-gray-300 dark:text-gray-600 rtl:rotate-180" />
            <button type="button" class="shrink-0 max-w-[160px] truncate px-1 py-0.5 rounded hover:bg-gray-100 dark:hover:bg-gray-800"
                    :class="i === segments.length - 1 && !opened ? 'font-medium text-gray-800 dark:text-gray-200' : 'text-gray-500 dark:text-gray-400'"
                    :title="seg" @click="goTo(segments.slice(0, i + 1))">{{ seg }}</button>
          </template>
          <template v-if="opened">
            <UIcon name="i-heroicons-chevron-right" class="w-3 h-3 shrink-0 text-gray-300 dark:text-gray-600 rtl:rotate-180" />
            <span class="min-w-0 truncate px-1 py-0.5 font-medium text-gray-800 dark:text-gray-200" :title="fullPath(opened)"><bdi>{{ fileName(opened) }}</bdi></span>
          </template>
        </nav>
        <template v-if="opened">
          <a v-if="isWebUrl(opened.web_url)" :href="opened.web_url" target="_blank" rel="noopener noreferrer"
             class="shrink-0 h-7 px-2 inline-flex items-center gap-1 rounded-md border border-gray-200 dark:border-gray-700 text-xs text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800">
            <UIcon name="i-heroicons-arrow-top-right-on-square" class="w-3.5 h-3.5" />{{ $t('agentsPage.fileBrowserOpenInSource') }}
          </a>
          <button type="button" :title="$t('agentsPage.fileBrowserClosePreview')"
                  class="shrink-0 h-7 w-7 inline-flex items-center justify-center rounded-md border border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800"
                  @click="opened = null">
            <UIcon name="i-heroicons-x-mark" class="w-3.5 h-3.5" />
          </button>
        </template>
        <template v-else>
        <div class="relative w-44 shrink-0">
          <UIcon name="i-heroicons-magnifying-glass" class="absolute start-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400 pointer-events-none" />
          <input v-model="query" type="search" :placeholder="$t('agentsPage.fileBrowserSearch')"
                 class="w-full h-7 ps-7 pe-2 text-xs rounded-md border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-200 placeholder-gray-400 focus:outline-none focus:ring-1 focus:ring-blue-500" />
        </div>
        <button type="button" :disabled="loading" :title="$t('agentsPage.fileBrowserRefresh')"
                class="shrink-0 h-7 w-7 inline-flex items-center justify-center rounded-md border border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50"
                @click="load">
          <UIcon name="i-heroicons-arrow-path" class="w-3.5 h-3.5" :class="{ 'animate-spin': loading }" />
        </button>
        </template>
      </div>

      <!-- Listing a delegated source is a live call to the provider and can
           take a while; say so rather than render an empty browser. -->
      <ConnectionFilePreview v-if="opened" :key="opened.id" :ds-id="dsId" :connection-id="connectionId" :file="opened" @ready="revealPreview" />
      <div v-else-if="loading && !loaded" class="text-xs text-gray-400 dark:text-gray-500 py-2 flex items-center gap-1.5">
        <UIcon name="i-heroicons-arrow-path" class="w-3 h-3 animate-spin" />{{ $t('agentsPage.loadingFiles') }}
      </div>
      <div v-else-if="error !== null" class="text-xs text-red-700 dark:text-red-300 bg-red-50 dark:bg-red-900/20 rounded px-2 py-1.5 flex items-start justify-between gap-2">
        <span class="min-w-0 break-words">{{ $t('agentsPage.fileBrowserError') }} {{ error }}</span>
        <button type="button" class="shrink-0 underline hover:no-underline" @click="load">{{ $t('agentsPage.fileBrowserRetry') }}</button>
      </div>
      <template v-else>
        <div class="max-h-80 overflow-auto rounded-md border border-gray-100 dark:border-gray-800 divide-y divide-gray-100 dark:divide-gray-800">
          <template v-if="!searching">
            <button v-for="d in contents.folders" :key="'d:' + d.name" type="button"
                    class="w-full flex items-center gap-2 px-2.5 py-1.5 text-xs text-start hover:bg-gray-50 dark:hover:bg-gray-800/60"
                    @click="goTo(d.segments)">
              <UIcon name="i-heroicons-folder" class="w-3.5 h-3.5 shrink-0 text-amber-500" />
              <span class="flex-1 min-w-0 truncate text-gray-800 dark:text-gray-200">{{ d.name }}</span>
              <span class="shrink-0 text-gray-400 dark:text-gray-500">{{ $t('agentsPage.countFiles', { n: d.fileCount }, d.fileCount) }}</span>
              <UIcon name="i-heroicons-chevron-right" class="w-3 h-3 shrink-0 text-gray-300 dark:text-gray-600 rtl:rotate-180" />
            </button>
          </template>
          <!-- Rows open a preview only where the connector hands out original
               bytes; mail messages stay a plain list with "open in source". -->
          <div v-for="f in shownFiles" :key="f.id" :role="previewSupported ? 'button' : undefined" :tabindex="previewSupported ? 0 : undefined"
               class="group flex items-center gap-2 px-2.5 py-1.5 text-xs"
               :class="previewSupported && 'cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/60 focus:outline-none focus-visible:bg-gray-50 dark:focus-visible:bg-gray-800/60'"
               @click="previewSupported && openPreview(f)" @keydown.enter="previewSupported && openPreview(f)">
            <UIcon :name="fileIcon(f.mime_type, fileName(f))" class="w-3.5 h-3.5 shrink-0 text-gray-400 dark:text-gray-500" />
            <div class="flex-1 min-w-0">
              <!-- <bdi>: a file name or "4.1 KB" keeps its own direction on an RTL page. -->
              <div class="truncate text-gray-700 dark:text-gray-300" :title="fullPath(f)"><bdi>{{ fileName(f) }}</bdi></div>
              <!-- Search spans every folder, so say where each hit lives and let it jump there. -->
              <button v-if="searching && parentOf(f).length" type="button"
                      class="block max-w-full truncate text-[11px] text-gray-400 dark:text-gray-500 hover:text-blue-600 dark:hover:text-blue-400"
                      @click.stop="goTo(parentOf(f))" @keydown.enter.stop>{{ parentOf(f).join(' / ') }}</button>
            </div>
            <span class="shrink-0 w-16 text-end tabular-nums text-gray-400 dark:text-gray-500"><bdi>{{ formatBytes(f.size) }}</bdi></span>
            <span class="shrink-0 w-24 text-end text-gray-400 dark:text-gray-500 hidden sm:inline">{{ formatDate(f.modified_at) }}</span>
            <a v-if="isWebUrl(f.web_url)" :href="f.web_url" target="_blank" rel="noopener noreferrer" :title="$t('agentsPage.fileBrowserOpenInSource')"
               class="shrink-0 text-gray-300 dark:text-gray-600 hover:text-blue-600 dark:hover:text-blue-400 opacity-0 group-hover:opacity-100 focus:opacity-100"
               @click.stop @keydown.enter.stop>
              <UIcon name="i-heroicons-arrow-top-right-on-square" class="w-3.5 h-3.5" />
            </a>
            <span v-else class="shrink-0 w-3.5" />
          </div>
          <div v-if="searching && results.length === 0" class="px-2.5 py-3 text-xs text-gray-400 dark:text-gray-500 italic">
            {{ $t('agentsPage.fileBrowserNoResults', { q: query.trim() }) }}
          </div>
          <div v-else-if="files.length === 0" class="px-2.5 py-3 text-xs text-gray-400 dark:text-gray-500 italic">
            {{ $t('agentsPage.fileBrowserEmpty') }}
          </div>
        </div>
        <div class="mt-1.5 text-[11px] text-gray-400 dark:text-gray-500">
          {{ $t('agentsPage.fileBrowserSummary', { n: total }) }}
        </div>
        <div v-if="total > files.length" class="mt-1 text-[11px] text-amber-700 dark:text-amber-300">
          {{ $t('agentsPage.fileBrowserTruncated', { shown: files.length, total }) }}
        </div>
      </template>
    </template>
  </div>
</template>

<script setup lang="ts">
import ConnectionFilePreview from '~/components/datasources/ConnectionFilePreview.vue'
import {
  buildFileTree, fileIcon, fileName, folderAt, folderContents, formatBytes, pathSegments, searchFiles,
  type BrowseFile,
} from '~/utils/fileTree'

const props = defineProps<{ dsId: string; connectionId: string }>()
const { locale } = useI18n()

// The endpoint walks the whole scope on every call, so fetch it once, up to
// the server's cap, and navigate folders client-side.
const LIMIT = 5000

const files = ref<BrowseFile[]>([])
const total = ref(0)
const loading = ref(false)
const loaded = ref(false)
const error = ref<string | null>(null)
const connectRequired = ref(false)
const previewSupported = ref(true)
const segments = ref<string[]>([])
const query = ref('')
const opened = ref<BrowseFile | null>(null)

const tree = computed(() => buildFileTree(files.value))
const current = computed(() => folderAt(tree.value, segments.value) || tree.value)
const contents = computed(() => folderContents(current.value))
const searching = computed(() => query.value.trim().length > 0)
const results = computed(() => searchFiles(files.value, query.value))
const shownFiles = computed(() => (searching.value ? results.value : contents.value.files))

const fullPath = (f: BrowseFile) => pathSegments(f).join('/')
const parentOf = (f: BrowseFile) => pathSegments(f).slice(0, -1)
const isWebUrl = (u?: string | null) => !!u && /^https?:\/\//i.test(u)
const formatDate = (v?: string | null) => {
  if (!v) return ''
  const d = new Date(v)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString(locale.value, { year: 'numeric', month: 'short', day: 'numeric' })
}

function goTo(segs: string[]) {
  segments.value = segs
  query.value = ''
  opened.value = null
}

// Open in place of the listing, with the breadcrumbs moved to the file's own
// folder (a search hit may live anywhere) so closing lands next to it.
function openPreview(f: BrowseFile) {
  goTo(parentOf(f))
  opened.value = f
  nextTick(revealPreview)
}

// Scroll the pane so the preview's toolbar sits at its top and the document
// gets the whole visible height. Only the nearest scrolling ancestor moves —
// scrollIntoView would also shift overflow-hidden layout containers. Runs on
// open and again once the content has loaded (only then is the pane tall
// enough to scroll that far).
const toolbarEl = ref<HTMLElement | null>(null)
function revealPreview() {
  const el = toolbarEl.value
  if (!el || !opened.value) return
  for (let p = el.parentElement; p; p = p.parentElement) {
    const overflowY = getComputedStyle(p).overflowY
    if ((overflowY === 'auto' || overflowY === 'scroll') && p.scrollHeight > p.clientHeight) {
      const top = el.getBoundingClientRect().top - p.getBoundingClientRect().top + p.scrollTop - 12
      p.scrollTo({ top: Math.max(0, top), behavior: 'smooth' })
      return
    }
  }
}

let requestSeq = 0
async function load() {
  if (!props.dsId || !props.connectionId) return
  const seq = ++requestSeq
  loading.value = true
  error.value = null
  try {
    const res = await useMyFetch(`/data_sources/${props.dsId}/connections/${props.connectionId}/files?limit=${LIMIT}`, { method: 'GET' })
    if (seq !== requestSeq) return
    if (res.error.value) {
      const detail = (res.error.value as any)?.data?.detail
      error.value = typeof detail === 'string' ? detail : ''
      return
    }
    const d: any = res.data.value || {}
    connectRequired.value = !!d.connect_required
    previewSupported.value = d.preview_supported !== false
    files.value = d.files || []
    total.value = d.total ?? files.value.length
    loaded.value = true
    // Keep the open folder across a refresh unless it no longer exists.
    if (!folderAt(tree.value, segments.value)) segments.value = []
  } catch (e: any) {
    if (seq === requestSeq) error.value = e?.message || ''
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

watch(() => [props.dsId, props.connectionId], () => {
  segments.value = []
  query.value = ''
  opened.value = null
  loaded.value = false
  load()
}, { immediate: true })
</script>
