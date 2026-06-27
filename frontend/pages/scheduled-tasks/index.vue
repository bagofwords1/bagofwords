<template>
  <div class="py-6">
    <div class="max-w-3xl mx-auto px-4">
      <!-- Full-page empty state (no tasks, no active search) -->
      <div v-if="!isLoading && tasks.length === 0 && !searchTerm" class="flex flex-col items-center justify-center text-center py-20 px-4">
        <img src="/assets/empty-states/empty-pond.png" alt="" class="w-full max-w-sm opacity-90 select-none pointer-events-none" />
        <h3 class="mt-2 text-sm font-medium text-gray-900 dark:text-white">{{ $t('scheduled.empty') }}</h3>
        <p class="mt-1 max-w-xs text-xs leading-relaxed text-gray-500 dark:text-gray-400">{{ $t('scheduled.emptyDescription') }}</p>
        <button
          @click="openNewTask"
          :disabled="creatingTask"
          class="mt-5 inline-flex items-center gap-1.5 h-8 px-3 rounded-md bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 transition-colors disabled:opacity-50"
        >
          <Spinner v-if="creatingTask" class="w-3 h-3 animate-spin" />
          <UIcon v-else name="heroicons-plus" class="w-3.5 h-3.5" />
          {{ creatingTask ? $t('scheduled.creating') : $t('scheduled.newTask') }}
        </button>
      </div>

      <template v-else>
      <div class="mb-5">
        <div class="flex items-center justify-between">
          <h1 class="text-lg font-semibold text-gray-900 dark:text-white">
            <GoBackChevron v-if="isExcel" />
            {{ $t('scheduled.title') }}
          </h1>
          <button
            @click="openNewTask"
            :disabled="creatingTask"
            class="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-blue-500 hover:bg-blue-600 rounded-md transition-colors disabled:opacity-50"
          >
            <Spinner v-if="creatingTask" class="w-3 h-3 animate-spin" />
            <UIcon v-else name="heroicons-plus" class="w-3.5 h-3.5" />
            {{ creatingTask ? $t('scheduled.creating') : $t('scheduled.newTask') }}
          </button>
        </div>

        <div class="mt-3 flex items-center gap-2">
          <input v-model="searchTerm" type="text" :placeholder="$t('scheduled.searchPlaceholder')" class="w-full text-sm border rounded px-3 py-2 dark:bg-gray-800 dark:border-gray-700 dark:text-gray-100 dark:placeholder-gray-500" />
        </div>
      </div>

      <!-- Loading -->
      <div v-if="isLoading" class="text-xs text-gray-500 dark:text-gray-400 inline-flex items-center">
        <Spinner class="me-1" /> {{ $t('scheduled.loading') }}
      </div>

      <!-- No matches for the current search (page chrome stays visible) -->
      <div v-else-if="tasks.length === 0" class="py-12 text-center text-xs text-gray-500 dark:text-gray-400">
        {{ $t('scheduled.empty') }}
      </div>

      <!-- Task cards -->
      <div v-else class="space-y-3">
        <div
          v-for="task in tasks"
          :key="task.id"
          class="border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900 rounded-lg p-4 hover:shadow-md hover:border-gray-200 dark:hover:border-gray-700 transition-all cursor-pointer"
          @click="openTask(task)"
        >
          <div class="group flex items-start justify-between gap-3">
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2 mb-1">
                <span
                  class="text-[10px] px-1.5 py-0.5 rounded border"
                  :class="task.is_active
                    ? 'text-green-700 border-green-200 bg-green-50'
                    : 'text-gray-700 dark:text-gray-300 border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800'"
                >{{ task.is_active ? $t('scheduled.active') : $t('scheduled.paused') }}</span>
                <span class="text-[11px] text-gray-400 dark:text-gray-500">{{ getCronLabel(task.cron_schedule) }}</span>
                <span v-if="task.last_run_at" class="text-[11px] text-gray-400 dark:text-gray-500">&middot; {{ $t('scheduled.lastRun', { time: formatRelativeTime(task.last_run_at) }) }}</span>
              </div>
              <div class="text-sm font-medium text-gray-900 dark:text-white mb-1 line-clamp-2">{{ task.prompt?.content || $t('scheduled.untitledTask') }}</div>
              <div class="flex items-center gap-3 mt-2">
                <NuxtLink
                  :to="`/reports/${task.report_id}`"
                  class="text-[11px] text-blue-500 hover:text-blue-600 flex items-center gap-1"
                  @click.stop
                >
                  <UIcon name="heroicons-chat-bubble-left-right" class="w-3 h-3" />
                  {{ task.report?.title || $t('scheduled.untitledReport') }}
                </NuxtLink>
                <span v-if="task.user_name" class="text-[11px] text-gray-400 dark:text-gray-500">{{ $t('scheduled.by', { name: task.user_name }) }}</span>
              </div>
            </div>
            <div class="shrink-0">
              <UTooltip :text="$t('scheduled.delete')">
                <button
                  @click.stop="deleteTask(task)"
                  :disabled="deletingId === task.id"
                  class="p-1 rounded text-gray-300 dark:text-gray-600 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-950/40 opacity-0 group-hover:opacity-100 focus:opacity-100 transition-all disabled:opacity-50"
                >
                  <Spinner v-if="deletingId === task.id" class="w-3.5 h-3.5 animate-spin" />
                  <UIcon v-else name="heroicons-trash" class="w-3.5 h-3.5" />
                </button>
              </UTooltip>
            </div>
          </div>
        </div>
      </div>

      <!-- Results summary -->
      <div v-if="!isLoading && tasks.length > 0" class="mt-6 text-center text-[11px] text-gray-500 dark:text-gray-400">
        {{ $t(pagination.total === 1 ? 'scheduled.showingOne' : 'scheduled.showingMany', { shown: tasks.length, total: pagination.total }) }}
      </div>

      <!-- Load more -->
      <div v-if="pagination.has_next" class="mt-4 text-center">
        <button
          @click="loadMore"
          :disabled="isLoadingMore"
          class="text-xs px-3 py-1.5 rounded border border-gray-200 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800/50 text-gray-600 dark:text-gray-400 disabled:opacity-50"
        >
          <template v-if="isLoadingMore"><Spinner class="w-3 h-3 inline me-1" /> {{ $t('scheduled.loading') }}</template>
          <template v-else>{{ $t('scheduled.loadMore') }}</template>
        </button>
      </div>
      </template>
    </div>

    <!-- Scheduled Prompt Modal -->
    <ScheduledPromptModal
      v-if="modalReportId"
      v-model="showModal"
      :report-id="modalReportId"
      :scheduled-prompt="editingTask"
      @saved="onTaskSaved"
    />
  </div>
</template>

<script setup lang="ts">
import GoBackChevron from '@/components/excel/GoBackChevron.vue'
import Spinner from '~/components/Spinner.vue'
import ScheduledPromptModal from '~/components/ScheduledPromptModal.vue'

definePageMeta({ auth: true })

const toast = useToast()
const { t } = useI18n()
const { isExcel } = useExcel()
const { selectedAgentObjects } = useAgent()

const tasks = ref<any[]>([])
const isLoading = ref(true)
const isLoadingMore = ref(false)
const currentPage = ref(1)
const pagination = ref({ total: 0, page: 1, limit: 20, total_pages: 0, has_next: false, has_prev: false })
const searchTerm = ref('')

// Scheduled prompt modal (shared for create + edit)
const showModal = ref(false)
const modalReportId = ref<string | null>(null)
const editingTask = ref<any | null>(null)
const creatingTask = ref(false)
const deletingId = ref<string | null>(null)

const openTask = (task: any) => {
  editingTask.value = task
  modalReportId.value = task.report_id
  showModal.value = true
}

const openNewTask = async () => {
  if (creatingTask.value) return
  creatingTask.value = true
  try {
    const dataSourceIds = selectedAgentObjects.value.map((a: any) => a.id)
    const response = await useMyFetch('/reports', {
      method: 'POST',
      body: JSON.stringify({
        title: t('scheduled.defaultTitle'),
        files: [],
        data_sources: dataSourceIds,
      }),
    })
    if ((response as any).error?.value) throw new Error('Report creation failed')
    const data = ((response as any).data?.value) as any
    editingTask.value = null
    modalReportId.value = data.id
    showModal.value = true
  } catch {
    toast.add({ title: t('common.error'), description: t('scheduled.createFailed'), color: 'red' })
  } finally {
    creatingTask.value = false
  }
}

const onTaskSaved = () => {
  showModal.value = false
  currentPage.value = 1
  fetchTasks(1, searchTerm.value)
}

const deleteTask = async (task: any) => {
  if (deletingId.value) return
  if (!confirm(t('scheduled.deleteConfirm'))) return
  deletingId.value = task.id
  try {
    const response = await useMyFetch(`/reports/${task.report_id}/scheduled-prompts/${task.id}`, {
      method: 'DELETE',
    })
    if ((response as any).error?.value) throw new Error('Delete failed')
    // Drop it locally so the list updates without a full refetch.
    tasks.value = tasks.value.filter((t: any) => t.id !== task.id)
    pagination.value.total = Math.max(0, (pagination.value.total || 1) - 1)
    toast.add({ title: t('scheduled.toastDeleted'), color: 'green' })
  } catch (error) {
    console.error('Error deleting scheduled task:', error)
    toast.add({ title: t('common.error'), description: t('scheduled.deleteFailed'), color: 'red' })
  } finally {
    deletingId.value = null
  }
}

const fetchTasks = async (page: number = 1, search: string = '') => {
  if (page === 1) isLoading.value = true
  try {
    const response = await useMyFetch('/scheduled-prompts', {
      method: 'GET',
      query: {
        page,
        limit: pagination.value.limit,
        filter: 'my',
        search: search?.trim() || undefined,
      },
    })
    if (response.status.value === 'success' && response.data.value) {
      const data = response.data.value as any
      if (page === 1) {
        tasks.value = data.scheduled_prompts
      } else {
        tasks.value = [...tasks.value, ...data.scheduled_prompts]
      }
      pagination.value = data.meta
    } else {
      throw new Error('Could not fetch scheduled tasks')
    }
  } catch (error) {
    console.error('Error fetching scheduled tasks:', error)
    if (page === 1) tasks.value = []
  } finally {
    isLoading.value = false
    isLoadingMore.value = false
  }
}

const loadMore = async () => {
  isLoadingMore.value = true
  currentPage.value++
  await fetchTasks(currentPage.value, searchTerm.value)
}

function formatRelativeTime(dateStr: string): string {
  const date = new Date(dateStr)
  const diff = Math.max(0, Date.now() - date.getTime())
  const mins = Math.floor(diff / 60000)
  if (mins < 60) return t('queries.timeMinutesAgo', { n: mins })
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return t('queries.timeHoursAgo', { n: hrs })
  const days = Math.floor(hrs / 24)
  return t('queries.timeDaysAgo', { n: days })
}

const { getCronLabel } = useCronLabel()

let _searchTimer: any = null
watch(searchTerm, () => {
  if (_searchTimer) clearTimeout(_searchTimer)
  _searchTimer = setTimeout(() => {
    currentPage.value = 1
    fetchTasks(1, searchTerm.value)
  }, 300)
})

onMounted(async () => {
  await nextTick()
  await fetchTasks(1, '')
})
</script>

<style scoped>
.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
