<template>
  <div ref="rootEl" class="w-full relative group/preview">
    <!-- Opens the same file in the side panel. Hidden where no panel exists
         (the share view), so the card never offers an action that does
         nothing — `canExpand` is opt-in per host page. Always visible: the
         inline frame hides the viewer's toolbar, so this is the one
         affordance for reading the document at full size. -->
    <button
      v-if="canExpand && !expanded"
      type="button"
      class="absolute top-2 end-2 z-10 p-1 rounded-md bg-white/90 dark:bg-gray-800/90 border border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400 shadow-sm transition-colors hover:text-gray-700 dark:hover:text-gray-200"
      :title="$t('filePreview.openInPanel')"
      :aria-label="$t('filePreview.openInPanel')"
      @click="$emit('expand')"
    >
      <Icon name="heroicons:arrows-pointing-out" class="w-3.5 h-3.5" />
    </button>

    <!-- Document: the browser's own PDF viewer, opened at the page that was
         read. Office files reach here only once converted server-side. -->
    <template v-if="kind === 'pdf'">
      <!-- Placeholder only while the token is still being minted. When the
           mint failed outright, render nothing: the card's text path still
           exists, and a permanent frame-sized "unavailable" box is worse
           than no preview. -->
      <div
        v-if="!embedUrl && (pending || !visible)"
        class="flex items-center justify-center rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/50"
        :style="{ height: frameHeight }"
      >
        <Spinner class="w-4 h-4 text-gray-400" />
      </div>
      <iframe
        v-else-if="embedUrl"
        :src="frameSrc"
        :title="name || $t('filePreview.document')"
        class="w-full rounded-lg border border-gray-200 dark:border-gray-700 bg-white"
        :style="{ height: frameHeight }"
      />
    </template>

    <!-- Pictures, and page renders of a document we could not show natively. -->
    <template v-else-if="kind === 'image'">
      <div class="flex flex-col gap-1.5">
        <AuthenticatedImage
          v-if="visible && activeImageId"
          :key="activeImageId"
          :file-id="activeImageId"
          :alt="name || ''"
          :img-class="[
            'rounded-lg border border-gray-200 dark:border-gray-700 object-contain cursor-zoom-in bg-white',
            expanded ? 'max-h-[70vh] w-auto' : 'max-h-60 w-auto',
          ].join(' ')"
          @click="$emit('open', activeImageId)"
        />

        <!-- Page strip: only when there is more than one page to move between. -->
        <div
          v-if="!visible"
          class="flex items-center justify-center rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/50 h-40"
        >
          <Spinner class="w-4 h-4 text-gray-400" />
        </div>

        <div v-if="images.length > 1" class="flex items-center gap-1 flex-wrap">
          <button
            v-for="(id, i) in images"
            :key="id"
            type="button"
            class="px-1.5 py-0.5 rounded text-[10px] border transition-colors"
            :class="id === activeImageId
              ? 'border-blue-400 text-blue-600 bg-blue-50 dark:bg-blue-900/30'
              : 'border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800'"
            @click="activeIndex = i"
          >
            {{ $t('filePreview.pageN', { n: i + 1 }) }}
          </button>
          <span v-if="truncated && pagesTotal" class="text-[10px] text-gray-400 dark:text-gray-500 ms-1">
            {{ $t('filePreview.ofTotalPages', { n: images.length, total: pagesTotal }) }}
          </span>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import Spinner from '~/components/Spinner.vue'
import AuthenticatedImage from '~/components/AuthenticatedImage.vue'

const props = withDefaults(defineProps<{
  /** What to render. Comes from the backend's `preview.kind` — never inferred. */
  kind: 'pdf' | 'image'
  /** File to show. For 'image' this is the first of `imageFileIds`. */
  fileId?: string | null
  imageFileIds?: string[] | null
  /** 1-based page the document viewer should open at. */
  targetPage?: number | null
  pagesTotal?: number | null
  /** Fewer pages rendered than the document has. */
  truncated?: boolean
  name?: string | null
  expanded?: boolean
  /** Show the "open in side panel" affordance. Off unless the host page has
   *  a panel to open into. */
  canExpand?: boolean
}>(), {
  fileId: null,
  imageFileIds: null,
  targetPage: 1,
  pagesTotal: null,
  truncated: false,
  name: '',
  expanded: false,
  canExpand: false,
})

defineEmits<{
  (e: 'open', fileId: string): void
  (e: 'expand'): void
}>()

const { getEmbedUrl, msUntilRefresh } = useFileEmbedUrl()

const embedUrl = ref('')
const pending = ref(false)

const images = computed<string[]>(() => props.imageFileIds || (props.fileId ? [props.fileId] : []))
const activeIndex = ref(0)
const activeImageId = computed(() => images.value[activeIndex.value] || images.value[0] || '')

// Inline (card) frames are a compact glance at the document; the side
// panel (`expanded`) is where it is actually read.
const frameHeight = computed(() => (props.expanded ? '70vh' : '200px'))
// PDF Open Parameters, read by the browser's built-in viewer:
//   page      open at the page the model actually read
//   view=FitH fit the page WIDTH to the frame, not a zoomed-in corner
//   navpanes=0 hide the thumbnail rail — it opens by default and eats roughly
//             half a panel-sized frame, squeezing the document into what is
//             left. Useless for the single-page documents this mostly shows,
//             and the toolbar already carries page navigation.
//   toolbar=0 (inline only) the card is a glance, not a reader — the zoom /
//             rotate / summarize bar dominates a 200px frame. The side panel
//             keeps it. Chromium honors this; other viewers ignore it.
const frameSrc = computed(() =>
  embedUrl.value
    ? `${embedUrl.value}#page=${props.targetPage || 1}&view=FitH&navpanes=0`
      + (props.expanded ? '' : '&toolbar=0')
    : ''
)

// Lazy: a 30-turn conversation must not open 30 PDF frames on load. The frame
// is only fetched once its card is near the viewport.
const rootEl = ref<HTMLElement | null>(null)
const visible = ref(false)
let observer: IntersectionObserver | null = null

function stopObserving() {
  observer?.disconnect()
  observer = null
}

onMounted(() => {
  if (!rootEl.value || typeof IntersectionObserver === 'undefined') {
    visible.value = true
    return
  }
  observer = new IntersectionObserver(
    (entries) => {
      if (entries.some((e) => e.isIntersecting)) {
        visible.value = true
        stopObserving()
      }
    },
    { rootMargin: '300px' },
  )
  observer.observe(rootEl.value)
})

// A mounted iframe keeps whatever token URL it was given, and `visible` never
// flips back to false — so without this timer nothing would ever re-run load()
// and a tab left open past the token's TTL showed a dead 403 frame. Re-mint
// just after the cache's freshness window closes; the iframe src swaps
// reactively through frameSrc.
let refreshTimer: ReturnType<typeof setTimeout> | null = null

function clearRefreshTimer() {
  if (refreshTimer) {
    clearTimeout(refreshTimer)
    refreshTimer = null
  }
}

onUnmounted(() => {
  stopObserving()
  clearRefreshTimer()
})

async function load() {
  clearRefreshTimer()
  if (!visible.value || props.kind !== 'pdf' || !props.fileId) return
  pending.value = true
  try {
    embedUrl.value = await getEmbedUrl(props.fileId)
  } finally {
    pending.value = false
  }
  if (embedUrl.value && props.fileId) {
    // +1s past the freshness cutoff so getEmbedUrl sees a stale entry and
    // mints a new token instead of serving the same one back.
    refreshTimer = setTimeout(load, msUntilRefresh(props.fileId) + 1000)
  }
}

watch([visible, () => props.fileId, () => props.kind], load, { immediate: true })
watch(() => props.imageFileIds, () => { activeIndex.value = 0 })
</script>
