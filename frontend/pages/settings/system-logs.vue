<template>
  <div data-testid="system-logs-page">
    <!-- Delete-all sits in the page header (settings shell). -->
    <Teleport to="#settings-page-actions" defer>
      <UButton
        color="red"
        variant="soft"
        size="xs"
        icon="i-heroicons-trash"
        data-testid="system-logs-delete-all"
        :disabled="!status || deleting"
        :loading="deleting"
        @click="showDeleteModal = true"
      >
        {{ $t('settings.systemLogs.deleteAll') }}
      </UButton>
    </Teleport>

    <div v-if="loadError" class="rounded-lg border border-line p-4 text-xs text-red-600 dark:text-red-400" data-testid="system-logs-load-error">
      {{ loadError }}
    </div>

    <template v-else>
      <!-- Where the logs are kept. Recording is always on. -->
      <div v-if="status" class="rounded-lg border border-line bg-surface p-4 mb-4 flex flex-col gap-1.5">
        <p class="text-xs text-ink-2" data-testid="system-logs-retention">
          {{ $t('settings.systemLogs.retention', { days: status.retention_days, used: formatBytes(status.size_bytes), max: formatBytes(status.max_size_bytes) }) }}
        </p>
        <p v-if="status.write_error" class="text-2xs text-red-600 dark:text-red-400" data-testid="system-logs-write-error">
          {{ $t('settings.systemLogs.writeError', { directory: status.directory }) }}
        </p>
        <p v-if="status.dropped_lines" class="text-2xs text-red-600 dark:text-red-400" data-testid="system-logs-dropped">
          {{ $t('settings.systemLogs.droppedNotice', { count: status.dropped_lines }) }}
        </p>
      </div>

      <!-- Filters -->
      <div class="mb-3 flex items-end gap-2 flex-wrap">
        <UInput
          v-model="search"
          size="xs"
          icon="i-heroicons-magnifying-glass"
          class="flex-1 min-w-[180px] max-w-[280px]"
          :placeholder="$t('settings.systemLogs.searchPlaceholder')"
          data-testid="system-logs-search"
          @update:model-value="debouncedFetch"
        />
        <label class="flex flex-col gap-0.5 text-2xs text-ink-muted">
          {{ $t('settings.systemLogs.from') }}
          <UInput v-model="from" type="datetime-local" step="1" size="xs" data-testid="system-logs-from" />
        </label>
        <label class="flex flex-col gap-0.5 text-2xs text-ink-muted">
          {{ $t('settings.systemLogs.to') }}
          <UInput v-model="to" type="datetime-local" step="1" size="xs" data-testid="system-logs-to" />
        </label>
        <USelect
          v-model="instance"
          size="xs"
          :options="instanceOptions"
          option-attribute="label"
          value-attribute="value"
          data-testid="system-logs-instance"
        />
        <label class="flex items-center gap-1.5 text-xs text-ink-2 py-1">
          <UToggle v-model="errorsOnly" size="sm" data-testid="system-logs-errors-only" />
          {{ $t('settings.systemLogs.errorsOnly') }}
        </label>
        <label class="flex items-center gap-1.5 text-xs text-ink-2 py-1">
          <UToggle v-model="autoRefresh" size="sm" data-testid="system-logs-auto-refresh" />
          {{ $t('settings.systemLogs.autoRefresh') }}
        </label>
        <UButton
          v-if="hasFilters"
          size="xs"
          variant="ghost"
          color="gray"
          data-testid="system-logs-clear-filters"
          @click="clearFilters"
        >
          {{ $t('settings.systemLogs.clearFilters') }}
        </UButton>
        <UButton
          size="xs"
          variant="outline"
          color="gray"
          icon="i-heroicons-arrow-path"
          class="ms-auto"
          :loading="loading"
          data-testid="system-logs-refresh"
          @click="refreshAll"
        >
          {{ $t('settings.systemLogs.refresh') }}
        </UButton>
      </div>

      <p v-if="rangeError" class="mb-2 text-xs text-red-600 dark:text-red-400">{{ rangeError }}</p>

      <!-- Log text box. Logs are left-to-right text whatever the UI language. -->
      <div
        ref="logBox"
        dir="ltr"
        role="log"
        :aria-label="$t('settings.systemLogs.title')"
        data-testid="system-logs-box"
        class="rtl-no-flip h-[60vh] overflow-auto rounded-lg border border-line bg-surface-subtle font-mono text-xs leading-5 py-2"
        :class="loading && !entries.length ? 'opacity-60' : ''"
      >
        <div v-if="!entries.length" class="px-3 py-6 text-center text-ink-muted font-sans" data-testid="system-logs-empty">
          {{ $t('settings.systemLogs.empty') }}
        </div>
        <div
          v-for="entry in entries"
          :key="entry.id"
          data-testid="system-logs-line"
          :data-error="isError(entry) ? 'true' : 'false'"
          class="px-3 whitespace-pre-wrap break-words"
          :class="isError(entry) ? 'text-red-600 dark:text-red-400' : 'text-ink-2'"
        ><span :class="isError(entry) ? 'opacity-80' : 'text-ink-muted'">{{ formatTimestamp(entry.timestamp) }}</span>{{ ' ' }}<span v-if="showInstance" :class="isError(entry) ? 'opacity-80' : 'text-ink-muted'">[{{ entry.instance_id }}]{{ ' ' }}</span><span class="font-semibold">{{ entry.level || entry.stream.toUpperCase() }}</span>{{ ' ' }}<span v-if="entry.logger" :class="isError(entry) ? 'opacity-80' : 'text-ink-muted'">{{ entry.logger }}{{ ' ' }}</span>{{ entry.message }}</div>
      </div>

      <p class="mt-2 text-2xs text-ink-muted" data-testid="system-logs-count">
        {{ hasMore
          ? $t('settings.systemLogs.countTruncated', { count: entries.length })
          : $t('settings.systemLogs.count', { count: entries.length }) }}
      </p>
    </template>

    <UModal v-model="showDeleteModal" :ui="{ width: 'sm:max-w-md' }">
      <UCard>
        <template #header>
          <h3 class="text-sm font-semibold text-ink">{{ $t('settings.systemLogs.deleteTitle') }}</h3>
        </template>
        <p class="text-sm text-ink-2">{{ $t('settings.systemLogs.deleteMessage') }}</p>
        <template #footer>
          <div class="flex justify-end gap-2">
            <UButton size="xs" color="gray" variant="ghost" @click="showDeleteModal = false">
              {{ $t('settings.systemLogs.cancel') }}
            </UButton>
            <UButton size="xs" color="red" :loading="deleting" data-testid="system-logs-delete-confirm" @click="deleteAll">
              {{ $t('settings.systemLogs.deleteConfirm') }}
            </UButton>
          </div>
        </template>
      </UCard>
    </UModal>
  </div>
</template>

<script setup lang="ts">
definePageMeta({
  auth: true,
  permissions: ['full_admin_access'],
  middleware: () => {
    if (useAuth().data.value?.is_superuser !== true) return navigateTo('/')
  }
})

type LogEntry = {
  id: string
  timestamp: string
  instance_id: string
  level: string | null
  logger: string | null
  stream: 'stdout' | 'stderr'
  message: string
}

type LogStatus = {
  instance_id: string
  instances: string[]
  directory: string
  retention_days: number
  max_size_bytes: number
  size_bytes: number
  write_error: boolean
  dropped_lines: number
}

const { t, locale } = useI18n({ useScope: 'global' })
const toast = useToast()
const { getErrorMessage } = useErrorMessage()

const status = ref<LogStatus | null>(null)
const entries = ref<LogEntry[]>([])
const hasMore = ref(false)
const loading = ref(false)
const deleting = ref(false)
const loadError = ref<string | null>(null)
const rangeError = ref<string | null>(null)
const showDeleteModal = ref(false)
const logBox = ref<HTMLElement | null>(null)

const search = ref('')
const from = ref('')
const to = ref('')
const instance = ref('')
const errorsOnly = ref(false)
const autoRefresh = ref(false)

const instanceOptions = computed(() => [
  { value: '', label: t('settings.systemLogs.allInstances') },
  ...(status.value?.instances || []).map((id) => ({
    value: id,
    label: id === status.value?.instance_id ? t('settings.systemLogs.thisInstance', { id }) : id,
  })),
])

// The instance column only earns its width when there is more than one.
const showInstance = computed(() => (status.value?.instances.length || 0) > 1 && !instance.value)

const hasFilters = computed(() => !!(search.value || from.value || to.value || instance.value || errorsOnly.value))

// The API speaks naive UTC; mark it as such before the browser parses it.
function parseUtc(value: string): Date {
  return new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(value) ? value : `${value}Z`)
}

const pad = (n: number, w = 2) => String(n).padStart(w, '0')
// A log-style local timestamp: the From/To pickers are in the browser's local
// time too, so what you read is what you filter on.
function formatTimestamp(value: string): string {
  const d = parseUtc(value)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`
}

function formatBytes(bytes: number): string {
  const units = ['byte', 'kilobyte', 'megabyte', 'gigabyte'] as const
  let value = bytes
  let i = 0
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024
    i++
  }
  return new Intl.NumberFormat(locale.value, {
    style: 'unit',
    unit: units[i],
    unitDisplay: 'short',
    maximumFractionDigits: i >= 2 ? 1 : 0,
  }).format(value)
}

const isError = (e: LogEntry) => e.stream === 'stderr' || e.level === 'ERROR' || e.level === 'CRITICAL'

function toIso(local: string): string | undefined {
  if (!local) return undefined
  const d = new Date(local)
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString()
}

async function loadStatus() {
  const res = await useMyFetch('/api/system-logs/status')
  if (res.status.value === 'success') {
    status.value = res.data.value as LogStatus
    loadError.value = null
  } else {
    loadError.value = getErrorMessage(res.error.value, t('settings.systemLogs.loadFailed'))
  }
}

// Searches, filter changes and auto-refresh can overlap; only the newest
// request's answer is shown, so a slow earlier one can't replace it.
let latestRequest = 0

async function fetchLogs() {
  const start = toIso(from.value)
  const end = toIso(to.value)
  if (start && end && start > end) {
    rangeError.value = t('settings.systemLogs.rangeInvalid')
    return
  }
  rangeError.value = null
  const query: Record<string, string> = {}
  if (start) query.start = start
  if (end) query.end = end
  if (instance.value) query.instance_id = instance.value
  if (errorsOnly.value) query.errors_only = 'true'
  if (search.value.trim()) query.search = search.value.trim()

  const box = logBox.value
  const pinnedToBottom = !box || box.scrollHeight - box.scrollTop - box.clientHeight < 40
  const requestId = ++latestRequest
  loading.value = true
  try {
    const res = await useMyFetch('/api/system-logs', { query })
    if (requestId !== latestRequest) return
    if (res.status.value === 'success') {
      const data = res.data.value as { items: LogEntry[]; has_more: boolean }
      // The API returns newest first; read top to bottom like a terminal.
      entries.value = [...data.items].reverse()
      hasMore.value = data.has_more
      if (pinnedToBottom) {
        await nextTick()
        if (logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight
      }
    } else if (!loadError.value) {
      toast.add({ title: t('settings.systemLogs.loadFailed'), description: getErrorMessage(res.error.value), color: 'red' })
    }
  } finally {
    if (requestId === latestRequest) loading.value = false
  }
}

async function refreshAll() {
  await loadStatus()
  if (!loadError.value) await fetchLogs()
}

async function deleteAll() {
  deleting.value = true
  try {
    const res = await useMyFetch('/api/system-logs', { method: 'DELETE' })
    if (res.status.value === 'success') {
      showDeleteModal.value = false
      toast.add({ title: t('settings.systemLogs.deletedToast'), color: 'green' })
      await refreshAll()
    } else {
      toast.add({ title: t('settings.systemLogs.deleteFailed'), description: getErrorMessage(res.error.value), color: 'red' })
    }
  } finally {
    deleting.value = false
  }
}

function clearFilters() {
  // Resetting a watched filter already refetches; fetch here only when the
  // search box was the one thing set.
  const watchedSet = !!(from.value || to.value || instance.value || errorsOnly.value)
  search.value = ''
  from.value = ''
  to.value = ''
  instance.value = ''
  errorsOnly.value = false
  if (!watchedSet) fetchLogs()
}

let searchTimer: ReturnType<typeof setTimeout> | null = null
function debouncedFetch() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(fetchLogs, 300)
}

watch([errorsOnly, from, to, instance], fetchLogs)

let refreshTimer: ReturnType<typeof setInterval> | null = null
watch(autoRefresh, (on) => {
  if (refreshTimer) clearInterval(refreshTimer)
  refreshTimer = on ? setInterval(() => { if (!loading.value) fetchLogs() }, 5000) : null
})

onMounted(refreshAll)
onBeforeUnmount(() => {
  if (refreshTimer) clearInterval(refreshTimer)
  if (searchTimer) clearTimeout(searchTimer)
})
</script>
