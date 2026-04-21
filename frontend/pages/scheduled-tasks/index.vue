<template>
  <div class="py-6">
    <div class="max-w-3xl mx-auto px-4">
      <div class="mb-5">
        <div class="flex items-center justify-between">
          <h1 class="text-lg font-semibold text-gray-900">
            <GoBackChevron v-if="isExcel" />
            Scheduled Tasks
          </h1>
          <button
            @click="openNewTask"
            :disabled="creatingTask"
            class="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-blue-500 hover:bg-blue-600 rounded-md transition-colors disabled:opacity-50"
          >
            <Spinner v-if="creatingTask" class="w-3 h-3 animate-spin" />
            <UIcon v-else name="heroicons-plus" class="w-3.5 h-3.5" />
            {{ creatingTask ? 'Creating...' : 'New task' }}
          </button>
        </div>

        <div class="mt-3 flex items-center gap-2">
          <input v-model="searchTerm" type="text" placeholder="Search tasks..." class="w-full text-sm border rounded px-3 py-2" />
        </div>
      </div>

      <!-- Loading -->
      <div v-if="isLoading" class="text-xs text-gray-500 inline-flex items-center">
        <Spinner class="me-1" /> Loading...
      </div>

      <!-- Empty -->
      <div v-else-if="tasks.length === 0" class="flex flex-col items-center justify-center py-16 px-4">
        <div class="w-16 h-16 rounded-full bg-gray-50 flex items-center justify-center mb-4">
          <UIcon name="heroicons-clock" class="w-8 h-8 text-gray-400" />
        </div>
        <h3 class="text-sm font-medium text-gray-900 mb-1">No scheduled tasks</h3>
        <p class="text-xs text-gray-500 text-center max-w-sm">
          Create a scheduled task to automatically run prompts on a recurring basis.
        </p>
      </div>

      <!-- Task cards -->
      <div v-else class="space-y-3">
        <div
          v-for="task in tasks"
          :key="task.id"
          class="border border-gray-100 bg-white rounded-lg p-4 hover:shadow-md hover:border-gray-200 transition-all cursor-pointer"
          @click="navigateToReport(task.report_id)"
        >
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2 mb-1">
                <span
                  class="text-[10px] px-1.5 py-0.5 rounded border"
                  :class="task.is_active
                    ? 'text-green-700 border-green-200 bg-green-50'
                    : 'text-gray-700 border-gray-200 bg-gray-50'"
                >{{ task.is_active ? 'ACTIVE' : 'PAUSED' }}</span>
                <span class="text-[11px] text-gray-400">{{ getCronLabel(task.cron_schedule) }}</span>
                <span v-if="task.last_run_at" class="text-[11px] text-gray-400">&middot; Last run {{ formatRelativeTime(task.last_run_at) }}</span>
              </div>
              <div class="text-sm font-medium text-gray-900 mb-1 line-clamp-2">{{ task.prompt?.content || 'Untitled task' }}</div>
              <div class="flex items-center gap-3 mt-2">
                <a
                  :href="`/reports/${task.report_id}`"
                  class="text-[11px] text-blue-500 hover:text-blue-600 flex items-center gap-1"
                  @click.stop
                >
                  <UIcon name="heroicons-chat-bubble-left-right" class="w-3 h-3" />
                  {{ task.report?.title || 'Untitled report' }}
                </a>
                <span v-if="task.user_name" class="text-[11px] text-gray-400">by {{ task.user_name }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Results summary -->
      <div v-if="!isLoading && tasks.length > 0" class="mt-6 text-center text-[11px] text-gray-500">
        Showing {{ tasks.length }} of {{ pagination.total }} {{ pagination.total === 1 ? 'task' : 'tasks' }}
      </div>

      <!-- Load more -->
      <div v-if="pagination.has_next" class="mt-4 text-center">
        <button
          @click="loadMore"
          :disabled="isLoadingMore"
          class="text-xs px-3 py-1.5 rounded border border-gray-200 hover:bg-gray-50 text-gray-600 disabled:opacity-50"
        >
          <template v-if="isLoadingMore"><Spinner class="w-3 h-3 inline me-1" /> Loading...</template>
          <template v-else>Load more</template>
        </button>
      </div>
    </div>

    <!-- Scheduled Prompt Modal -->
    <ScheduledPromptModal
      v-if="newTaskReportId"
      v-model="showModal"
      :report-id="newTaskReportId"
      @saved="onTaskSaved"
    />
  </div>
</template>

<script setup lang="ts">
import GoBackChevron from '@/components/excel/GoBackChevron.vue'
import Spinner from '~/components/Spinner.vue'
import ScheduledPromptModal from '~/components/ScheduledPromptModal.vue'

definePageMeta({ auth: true })

const router = useRouter()
const toast = useToast()
const { isExcel } = useExcel()
const { selectedDomainObjects } = useDomain()

const tasks = ref<any[]>([])
const isLoading = ref(true)
const isLoadingMore = ref(false)
const currentPage = ref(1)
const pagination = ref({ total: 0, page: 1, limit: 20, total_pages: 0, has_next: false, has_prev: false })
const searchTerm = ref('')

// New task modal
const showModal = ref(false)
const newTaskReportId = ref<string | null>(null)
const creatingTask = ref(false)

const openNewTask = async () => {
  if (creatingTask.value) return
  creatingTask.value = true
  try {
    const dataSourceIds = selectedDomainObjects.value.map((ds: any) => ds.id)
    const response = await useMyFetch('/reports', {
      method: 'POST',
      body: JSON.stringify({
        title: 'Scheduled task',
        files: [],
        data_sources: dataSourceIds,
      }),
    })
    if ((response as any).error?.value) throw new Error('Report creation failed')
    const data = ((response as any).data?.value) as any
    newTaskReportId.value = data.id
    showModal.value = true
  } catch {
    toast.add({ title: 'Error', description: 'Failed to create task', color: 'red' })
  } finally {
    creatingTask.value = false
  }
}

const onTaskSaved = () => {
  showModal.value = false
  currentPage.value = 1
  fetchTasks(1, searchTerm.value)
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

const navigateToReport = (reportId: string) => {
  router.push(`/reports/${reportId}`)
}

function formatRelativeTime(dateStr: string): string {
  const date = new Date(dateStr)
  const diff = Math.max(0, Date.now() - date.getTime())
  const mins = Math.floor(diff / 60000)
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  return `${days}d ago`
}

function getCronLabel(cron?: string): string {
  if (!cron) return ''
  const parts = cron.split(' ')
  if (parts.length < 5) return cron
  const [min, hour, dom, , dow] = parts

  const isStep = (v: string) => v.startsWith('*/')
  const stepVal = (v: string) => parseInt(v.slice(2))

  if (isStep(min) && hour === '*') return `Every ${stepVal(min)} minutes`
  if (min !== '*' && isStep(hour)) return `Every ${stepVal(hour)} hours`

  if (min !== '*' && hour !== '*' && !isStep(hour)) {
    const h = parseInt(hour)
    const ampm = h >= 12 ? 'PM' : 'AM'
    const h12 = h === 0 ? 12 : h > 12 ? h - 12 : h
    const time = `${h12}:${min.padStart(2, '0')} ${ampm}`

    if (dow === '*' && dom === '*') return `Daily at ${time}`
    if (dow !== '*') {
      const dayNames: Record<string, string> = { '0': 'Sun', '1': 'Mon', '2': 'Tue', '3': 'Wed', '4': 'Thu', '5': 'Fri', '6': 'Sat' }
      const days = dow.split(',').map((d: string) => dayNames[d] || d).join(', ')
      return `${days} at ${time}`
    }
    return `Monthly on day ${dom} at ${time}`
  }

  return cron
}

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
