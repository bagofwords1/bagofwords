<template>
  <div class="mt-4" data-testid="system-logs-page">
    <div class="mb-4 flex items-start justify-between gap-4 flex-wrap">
      <div>
        <h2 class="text-sm font-medium text-ink">{{ $t('settings.systemLogs.title') }}</h2>
        <p class="text-xs text-ink-muted mt-0.5">{{ $t('settings.systemLogs.subtitle') }}</p>
      </div>
      <UButton
        color="red"
        variant="soft"
        size="xs"
        icon="i-heroicons-trash"
        data-testid="system-logs-delete-all"
        :disabled="!settings || deleting"
        :loading="deleting"
        @click="showDeleteModal = true"
      >
        {{ $t('settings.systemLogs.deleteAll') }}
      </UButton>
    </div>

    <div v-if="loadError" class="rounded-lg border border-line p-4 text-xs text-red-600 dark:text-red-400" data-testid="system-logs-load-error">
      {{ loadError }}
    </div>

    <template v-else>
      <!-- Capture switch -->
      <div class="rounded-lg border border-line bg-surface p-4 mb-4 flex items-center gap-4 flex-wrap">
        <UToggle
          :model-value="!!settings?.enabled"
          :disabled="!settings || switching"
          data-testid="system-logs-toggle"
          :aria-label="$t('settings.systemLogs.captureLabel')"
          @update:model-value="setEnabled"
        />
        <div class="min-w-0 flex-1">
          <p class="text-sm text-ink">{{ $t('settings.systemLogs.captureLabel') }}</p>
          <p class="text-xs text-ink-muted" data-testid="system-logs-status">{{ statusText }}</p>
        </div>
        <label v-if="!settings?.enabled" class="flex items-center gap-2 text-xs text-ink-muted">
          {{ $t('settings.systemLogs.durationLabel') }}
          <USelect
            v-model="duration"
            size="xs"
            :options="durationOptions"
            option-attribute="label"
            value-attribute="value"
            data-testid="system-logs-duration"
          />
        </label>
        <p v-if="settings" class="w-full text-2xs text-ink-muted">
          {{ $t('settings.systemLogs.retention', { days: settings.retention_days, used: formatBytes(settings.size_bytes), max: formatBytes(settings.max_size_bytes) }) }}
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
          {{ settings?.enabled ? $t('settings.systemLogs.emptyEnabled') : $t('settings.systemLogs.emptyDisabled') }}
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
  layout: 'settings'
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

type LogSettings = {
  enabled: boolean
  expires_at: string | null
  instance_id: string
  instances: string[]
  retention_days: number
  max_size_bytes: number
  size_bytes: number
}

const { t, locale } = useI18n({ useScope: 'global' })
const toast = useToast()
const { getErrorMessage } = useErrorMessage()
const _df = useFormatDate()

const settings = ref<LogSettings | null>(null)
const entries = ref<LogEntry[]>([])
const hasMore = ref(false)
const loading = ref(false)
const switching = ref(false)
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
const duration = ref<number>(60)

const durationOptions = computed(() => [
  { value: 15, label: t('settings.systemLogs.durations.m15') },
  { value: 60, label: t('settings.systemLogs.durations.h1') },
  { value: 240, label: t('settings.systemLogs.durations.h4') },
  { value: 1440, label: t('settings.systemLogs.durations.h24') },
  { value: 10080, label: t('settings.systemLogs.durations.d7') },
  { value: 0, label: t('settings.systemLogs.durations.untilOff') },
])

const instanceOptions = computed(() => [
  { value: '', label: t('settings.systemLogs.allInstances') },
  ...(settings.value?.instances || []).map((id) => ({
    value: id,
    label: id === settings.value?.instance_id ? t('settings.systemLogs.thisInstance', { id }) : id,
  })),
])

// The instance column only earns its width when there is more than one.
const showInstance = computed(() => (settings.value?.instances.length || 0) > 1 && !instance.value)

const hasFilters = computed(() => !!(search.value || from.value || to.value || instance.value || errorsOnly.value))

const statusText = computed(() => {
  const s = settings.value
  if (!s) return ''
  if (!s.enabled) return t('settings.systemLogs.statusOff')
  if (s.expires_at) return t('settings.systemLogs.statusUntil', { time: _df.formatDateTime(parseUtc(s.expires_at)) })
  return t('settings.systemLogs.statusOn')
})

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

async function loadSettings() {
  const res = await useMyFetch('/api/system-logs/settings')
  if (res.status.value === 'success') {
    settings.value = res.data.value as LogSettings
    loadError.value = null
  } else {
    loadError.value = getErrorMessage(res.error.value, t('settings.systemLogs.loadFailed'))
  }
}

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
  loading.value = true
  try {
    const res = await useMyFetch('/api/system-logs', { query })
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
    loading.value = false
  }
}

async function refreshAll() {
  await loadSettings()
  if (!loadError.value) await fetchLogs()
}

async function setEnabled(enabled: boolean) {
  switching.value = true
  try {
    const body: Record<string, unknown> = { enabled }
    if (enabled && duration.value) body.duration_minutes = duration.value
    const res = await useMyFetch('/api/system-logs/settings', { method: 'PUT', body })
    if (res.status.value === 'success') {
      settings.value = res.data.value as LogSettings
      toast.add({ title: enabled ? t('settings.systemLogs.enabledToast') : t('settings.systemLogs.disabledToast'), color: 'green' })
      await fetchLogs()
    } else {
      toast.add({ title: t('settings.systemLogs.saveFailed'), description: getErrorMessage(res.error.value), color: 'red' })
    }
  } finally {
    switching.value = false
  }
}

async function deleteAll() {
  deleting.value = true
  try {
    const res = await useMyFetch('/api/system-logs', { method: 'DELETE' })
    if (res.status.value === 'success') {
      const { deleted } = res.data.value as { deleted: number }
      showDeleteModal.value = false
      toast.add({ title: t('settings.systemLogs.deletedToast', { count: deleted }), color: 'green' })
      await refreshAll()
    } else {
      toast.add({ title: t('settings.systemLogs.deleteFailed'), description: getErrorMessage(res.error.value), color: 'red' })
    }
  } finally {
    deleting.value = false
  }
}

function clearFilters() {
  search.value = ''
  from.value = ''
  to.value = ''
  instance.value = ''
  errorsOnly.value = false
  fetchLogs()
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
