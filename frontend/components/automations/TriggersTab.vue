<template>
  <div>
    <!-- Empty state -->
    <div v-if="!isLoading && triggers.length === 0" class="flex flex-col items-center justify-center text-center py-20 px-4">
      <UIcon name="heroicons-bolt" class="w-10 h-10 text-amber-400" />
      <h3 class="mt-3 text-sm font-medium text-gray-900 dark:text-white">{{ $t('triggers.empty') }}</h3>
      <p class="mt-1 max-w-sm text-xs leading-relaxed text-gray-500 dark:text-gray-400">{{ $t('triggers.emptyDescription') }}</p>
      <button
        @click="openNew"
        class="mt-5 inline-flex items-center gap-1.5 h-8 px-3 rounded-md bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 transition-colors"
        data-testid="new-trigger-empty"
      >
        <UIcon name="heroicons-plus" class="w-3.5 h-3.5" />
        {{ $t('triggers.newTrigger') }}
      </button>
    </div>

    <template v-else>
      <div class="mb-5">
        <div class="flex items-center justify-between">
          <div class="text-xs text-gray-500 dark:text-gray-400">{{ $t('triggers.description') }}</div>
          <button
            @click="openNew"
            class="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-blue-500 hover:bg-blue-600 rounded-md transition-colors"
            data-testid="new-trigger"
          >
            <UIcon name="heroicons-plus" class="w-3.5 h-3.5" />
            {{ $t('triggers.newTrigger') }}
          </button>
        </div>

        <div class="mt-3 flex items-center gap-2">
          <input v-model="searchTerm" type="text" :placeholder="$t('triggers.searchPlaceholder')" class="w-full text-sm border rounded px-3 py-2 dark:bg-gray-800 dark:border-gray-700 dark:text-gray-100 dark:placeholder-gray-500" />
        </div>

        <!-- Status filter: All | Active | Paused -->
        <div class="mt-3 flex gap-0.5 p-0.5 bg-gray-100 dark:bg-gray-800 rounded w-fit">
          <button
            v-for="f in statusFilters"
            :key="f.value"
            class="px-2.5 py-1 text-[11px] rounded transition-colors"
            :class="statusFilter === f.value ? 'bg-white dark:bg-gray-900 text-gray-900 dark:text-white shadow-sm font-medium' : 'text-gray-400 hover:text-gray-600 dark:hover:text-gray-300'"
            @click="statusFilter = f.value"
          >
            {{ f.label }}
          </button>
        </div>
      </div>

      <!-- Loading -->
      <div v-if="isLoading" class="text-xs text-gray-500 dark:text-gray-400 inline-flex items-center">
        <Spinner class="me-1" /> {{ $t('triggers.loading') }}
      </div>

      <!-- No matches for the current search/filter (page chrome stays visible) -->
      <div v-else-if="visibleTriggers.length === 0" class="py-12 text-center text-xs text-gray-500 dark:text-gray-400">
        {{ $t('triggers.empty') }}
      </div>

      <!-- Trigger cards -->
      <div v-else class="space-y-3">
        <div
          v-for="trig in visibleTriggers"
          :key="trig.id"
          class="border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900 rounded-lg p-4 hover:shadow-md hover:border-gray-200 dark:hover:border-gray-700 transition-all"
          :data-testid="`trigger-card-${trig.name}`"
        >
          <div class="group flex items-start justify-between gap-3">
            <div class="min-w-0 flex-1 cursor-pointer" @click="openEdit(trig)">
              <div class="flex items-center gap-2 mb-1" :class="{ 'opacity-50': !trig.is_active }">
                <UIcon name="heroicons-bolt-solid" class="w-3.5 h-3.5 text-amber-500" />
                <span class="text-sm font-medium text-gray-900 dark:text-white">{{ trig.name || trig.task_template || $t('triggers.noTask') }}</span>
                <span v-if="trig.classify_enabled" class="text-[10px] px-1.5 py-0.5 rounded border text-purple-700 border-purple-200 bg-purple-50">AI {{ $t('triggers.filter') }}</span>
              </div>
              <div class="text-xs text-gray-500 dark:text-gray-400 line-clamp-1 mb-1.5">{{ trig.task_template || $t('triggers.noTask') }}</div>
              <div class="flex items-center gap-3 text-[11px] text-gray-400 dark:text-gray-500 flex-wrap">
                <span class="inline-flex items-center gap-1">
                  <UIcon name="heroicons-cube" class="w-3 h-3" />
                  {{ trig.data_sources.length ? trig.data_sources.map((d: any) => d.name).join(', ') : $t('triggers.noAgents') }}
                </span>
                <span class="inline-flex items-center gap-1">
                  <UIcon name="heroicons-cpu-chip" class="w-3 h-3" />
                  {{ modelName(trig.model_id) }}
                </span>
                <NuxtLink v-if="trig.project_id" :to="`/projects/${trig.project_id}`" class="inline-flex items-center gap-1 hover:text-gray-600 dark:hover:text-gray-300" @click.stop>
                  <UIcon name="heroicons-folder" class="w-3 h-3" />
                  {{ trig.project_name || $t('triggers.project') }}
                </NuxtLink>
                <span v-if="trig.last_delivery_at">&middot; {{ $t('triggers.lastDelivery', { time: formatRelativeTime(trig.last_delivery_at) }) }}</span>
              </div>
            </div>
            <div class="shrink-0 flex items-center gap-2">
              <UTooltip :text="trig.is_active ? $t('triggers.pause') : $t('triggers.resume')">
                <button
                  @click.stop="toggleActive(trig)"
                  :disabled="togglingId === trig.id"
                  class="relative inline-flex h-4 w-7 items-center rounded-full transition-colors disabled:opacity-50"
                  :class="trig.is_active ? 'bg-blue-500' : 'bg-gray-300 dark:bg-gray-700'"
                  :aria-pressed="trig.is_active"
                  :data-testid="`trigger-toggle-${trig.name}`"
                >
                  <span class="inline-block h-3 w-3 rounded-full bg-white transition-transform" :class="trig.is_active ? 'translate-x-3.5' : 'translate-x-0.5'" />
                </button>
              </UTooltip>
              <!-- Run count only: the history itself lives in the trigger's
                   summary, so there is one place to read it rather than two. -->
              <span
                class="inline-flex items-center gap-1 text-[11px]"
                :class="trig.last_run_status === 'error' ? 'text-red-500' : 'text-gray-400 dark:text-gray-500'"
                :data-testid="`trigger-runs-${trig.name}`"
              >
                <UIcon :name="trig.last_run_status === 'error' ? 'heroicons-exclamation-triangle' : 'heroicons-clock'" class="w-3 h-3" />
                {{ trig.last_run_status === 'error'
                    ? $t('triggers.runsLastFailed', { n: trig.run_count })
                    : $t('triggers.runs', { n: trig.run_count }) }}
              </span>
              <UTooltip :text="$t('triggers.copyUrl')">
                <button @click.stop="copy(trig.delivery_url, `url-${trig.id}`)" class="p-1 rounded text-gray-300 dark:text-gray-600 hover:text-gray-600" :data-testid="`trigger-copy-${trig.name}`">
                  <UIcon :name="copied === `url-${trig.id}` ? 'heroicons-check' : 'heroicons-link'" class="w-3.5 h-3.5" :class="copied === `url-${trig.id}` ? 'text-green-500' : ''" />
                </button>
              </UTooltip>
              <UTooltip :text="$t('triggers.delete')">
                <button @click.stop="removeTrigger(trig)" class="p-1 rounded text-gray-300 dark:text-gray-600 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-950/40">
                  <UIcon name="heroicons-trash" class="w-3.5 h-3.5" />
                </button>
              </UTooltip>
            </div>
          </div>

        </div>
      </div>
    </template>

    <!-- Trigger setup modal — shared with the project page, which opens it
         with a project so new triggers are filed there. -->
    <AutomationsTriggerModal
      v-model="showModal"
      :trigger="current"
      :models="models"
      @changed="fetchTriggers"
    />
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'

const toast = useToast()
const { t } = useI18n()

const triggers = ref<any[]>([])
const models = ref<any[]>([])
const isLoading = ref(true)
const togglingId = ref<string | null>(null)

// Search + status filter, client-side: the owner-scoped /triggers list is
// small and unpaginated, so no server round-trip is needed.
const searchTerm = ref('')
type StatusFilter = 'all' | 'active' | 'paused'
const statusFilter = ref<StatusFilter>('all')
const statusFilters = computed(() => [
  { value: 'all' as const, label: t('triggers.filterAll') },
  { value: 'active' as const, label: t('triggers.filterActive') },
  { value: 'paused' as const, label: t('triggers.filterPaused') },
])

const visibleTriggers = computed(() => {
  const q = searchTerm.value.trim().toLowerCase()
  return triggers.value.filter((trig: any) => {
    if (statusFilter.value === 'active' && !trig.is_active) return false
    if (statusFilter.value === 'paused' && trig.is_active) return false
    if (q && !`${trig.name || ''} ${trig.task_template || ''}`.toLowerCase().includes(q)) return false
    return true
  })
})

// The modal owns the whole setup flow (including provisioning a new trigger on
// open, which is what makes its delivery URL available immediately). This tab
// only says which trigger to open: null means "new".
const showModal = ref(false)
const current = ref<any | null>(null)

const { relativeTime: formatRelativeTime } = useRelativeTime()

function modelName(id: string | null): string {
  if (!id) return t('triggers.defaultModel')
  const m = models.value.find((m: any) => m.id === id)
  return m?.name || t('triggers.defaultModel')
}

async function fetchTriggers() {
  isLoading.value = true
  try {
    const { data } = await useMyFetch('/triggers')
    triggers.value = (data.value as any[]) || []
  } catch { triggers.value = [] } finally { isLoading.value = false }
}

async function fetchModels() {
  try {
    const { data } = await useMyFetch('/llm/models?is_enabled=true')
    models.value = ((data.value as any[]) || []).filter((m: any) => m.is_enabled !== false)
  } catch { models.value = [] }
}

function openNew() {
  current.value = null
  showModal.value = true
}

function openEdit(trig: any) {
  current.value = trig
  showModal.value = true
}

// Pause/resume in place. Optimistic: flip locally, revert on failure.
async function toggleActive(trig: any) {
  if (togglingId.value) return
  togglingId.value = trig.id
  const next = !trig.is_active
  trig.is_active = next
  try {
    const { error } = await useMyFetch(`/triggers/${trig.id}`, {
      method: 'PUT',
      body: { is_active: next },
    })
    if (error.value) throw error.value
  } catch (e) {
    console.error('toggle trigger failed', e)
    trig.is_active = !next
    toast.add({ title: t('common.error'), description: t('triggers.saveFailed'), color: 'red' })
  } finally {
    togglingId.value = null
  }
}

async function removeTrigger(trig: any) {
  if (!trig) return
  if (!confirm(t('triggers.deleteConfirm'))) return
  await useMyFetch(`/triggers/${trig.id}`, { method: 'DELETE' })
  triggers.value = triggers.value.filter((x: any) => x.id !== trig.id)
  toast.add({ title: t('triggers.toastDeleted'), color: 'green' })
}

const copied = ref<string | null>(null)
function copy(text: string, what: string = 'url') {
  if (!text) return
  navigator.clipboard.writeText(text)
  copied.value = what
  setTimeout(() => { if (copied.value === what) copied.value = null }, 1600)
}

onMounted(async () => {
  await Promise.all([fetchTriggers(), fetchModels()])
})
</script>

<style scoped>
.line-clamp-1 {
  display: -webkit-box;
  -webkit-line-clamp: 1;
  line-clamp: 1;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
