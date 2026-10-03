<template>
  <div class="mt-4">
    <div class="mb-4 flex items-end justify-between gap-4">
      <div>
        <h2 class="text-sm font-medium text-gray-900 dark:text-white">{{ $t('settings.audit.title') }}</h2>
        <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('settings.audit.subtitle') }}</p>
      </div>
      <div v-if="hasFeature('audit_logs') && streamsLicensed" class="inline-flex rounded border border-gray-200 dark:border-gray-700 p-0.5 text-xs" role="tablist">
        <button
          v-for="tb in (['activity', 'streams'] as const)"
          :key="tb"
          type="button"
          role="tab"
          :aria-selected="tab === tb"
          :data-testid="`audit-tab-${tb}`"
          class="px-2.5 py-1 rounded"
          :class="tab === tb ? 'bg-gray-900 text-white dark:bg-white dark:text-gray-900' : 'text-gray-600 dark:text-gray-400 hover:text-gray-900'"
          @click="setTab(tb)"
        >{{ $t(`settings.audit.tabs.${tb}`) }}</button>
      </div>
    </div>

    <!-- Enterprise Gate -->
    <template v-if="!hasFeature('audit_logs')">
      <div class="rounded border border-gray-200 dark:border-gray-700 p-4 bg-gray-50 dark:bg-gray-900">
        <p class="text-xs text-gray-600 dark:text-gray-400 mb-2">
          {{ $t('settings.audit.enterpriseRequired') }}
        </p>
        <a
          href="https://docs.bagofwords.com/enterprise"
          target="_blank"
          rel="noopener noreferrer"
          class="text-xs text-blue-600 hover:text-blue-700"
        >
          {{ $t('settings.audit.learnMore') }}
        </a>
      </div>
    </template>

    <AuditStreams v-else-if="tab === 'streams' && streamsLicensed" />

    <!-- Activity -->
    <template v-else>
      <div class="mb-3 flex items-center gap-2 flex-wrap">
        <div class="relative flex-1 min-w-[180px] max-w-[280px]">
          <input
            v-model="searchQuery"
            type="text"
            data-testid="audit-search"
            :placeholder="$t('settings.audit.searchPlaceholder')"
            class="w-full ps-7 pe-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded focus:outline-none focus:border-gray-400 bg-white dark:bg-gray-900"
            @input="debouncedSearch"
          />
          <UIcon name="i-heroicons-magnifying-glass" class="absolute start-2 top-1.5 w-3.5 h-3.5 text-gray-400" />
        </div>

        <AuditFilterSelect
          v-model="selectedActions"
          multiple
          testid="audit-filter-action"
          icon="i-heroicons-bolt"
          :label="$t('settings.audit.allActions')"
          :options="actionOptions"
          :search-placeholder="$t('settings.audit.filters.filterActions')"
          :empty-label="$t('settings.audit.filters.noOptions')"
        />
        <AuditFilterSelect
          v-model="selectedResource"
          testid="audit-filter-resource"
          icon="i-heroicons-cube"
          :label="$t('settings.audit.filters.resource')"
          :all-label="$t('settings.audit.filters.allResources')"
          :options="resourceOptions"
          :empty-label="$t('settings.audit.filters.noOptions')"
        />
        <AuditFilterSelect
          v-model="selectedUser"
          testid="audit-filter-user"
          icon="i-heroicons-user"
          :label="$t('settings.audit.filters.user')"
          :all-label="$t('settings.audit.filters.allUsers')"
          :options="userOptions"
          :empty-label="$t('settings.audit.filters.noOptions')"
        />
        <AuditFilterSelect
          v-model="selectedRange"
          testid="audit-filter-range"
          icon="i-heroicons-calendar"
          :searchable="false"
          :label="$t('settings.audit.filters.range')"
          :all-label="$t('settings.audit.filters.rangeAll')"
          :options="rangeOptions"
        />

        <button
          v-if="hasActiveFilters"
          class="text-xs text-gray-400 hover:text-gray-600"
          data-testid="audit-clear"
          @click="clearFilters"
        >
          {{ $t('settings.audit.clear') }}
        </button>

        <div ref="exportRef" class="relative ms-auto">
          <button
            type="button"
            data-testid="audit-export"
            class="flex items-center gap-1.5 px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded hover:border-gray-300 bg-white dark:bg-gray-900 text-gray-600 dark:text-gray-300"
            :disabled="exporting"
            @click="showExport = !showExport"
          >
            <UIcon :name="exporting ? 'i-heroicons-arrow-path' : 'i-heroicons-arrow-down-tray'" class="w-3.5 h-3.5" :class="exporting ? 'animate-spin' : ''" />
            {{ $t('settings.audit.export.button') }}
          </button>
          <div v-if="showExport" class="absolute top-full end-0 mt-1 w-60 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded shadow-md z-20 py-1">
            <button type="button" data-testid="audit-export-json" class="w-full text-start px-3 py-1.5 text-xs hover:bg-gray-50 dark:hover:bg-gray-800" @click="doExport('json')">{{ $t('settings.audit.export.json') }}</button>
            <button type="button" data-testid="audit-export-csv" class="w-full text-start px-3 py-1.5 text-xs hover:bg-gray-50 dark:hover:bg-gray-800" @click="doExport('csv')">{{ $t('settings.audit.export.csv') }}</button>
            <p class="px-3 pt-1 pb-1.5 text-[10px] text-gray-400 border-t border-gray-100 dark:border-gray-800 mt-1">{{ $t('settings.audit.export.hint') }}</p>
          </div>
        </div>
      </div>

      <!-- Loading State -->
      <div v-if="loading && !logs.length" class="py-8 text-center">
        <div class="inline-block w-4 h-4 border-2 border-gray-200 dark:border-gray-700 border-t-gray-500 rounded-full animate-spin"></div>
      </div>

      <!-- Error State -->
      <div v-else-if="error" class="py-6 text-center text-xs text-red-500">
        {{ error }}
      </div>

      <!-- Logs List -->
      <div v-else class="border border-gray-200 dark:border-gray-700 rounded overflow-hidden" :class="loading ? 'opacity-60' : ''">
        <template v-if="logs.length > 0">
          <div
            v-for="(log, idx) in logs"
            :key="log.id"
            data-testid="audit-row"
            role="button"
            tabindex="0"
            class="group flex items-center gap-3 px-3 py-2 text-xs cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800 focus:outline-none focus-visible:bg-gray-50 dark:focus-visible:bg-gray-800"
            :class="[{ 'border-t border-gray-100 dark:border-gray-800': idx > 0 }, selected?.id === log.id ? 'bg-blue-50/60 dark:bg-blue-950/40' : '']"
            @click="selected = log"
            @keydown.enter.prevent="selected = log"
          >
            <!-- Time -->
            <span class="w-[92px] shrink-0 text-gray-500 dark:text-gray-400 font-mono text-[11px] tabular-nums" :title="relativeTime(log.created_at)">
              {{ formatTimestamp(log.created_at) }}
            </span>

            <!-- Actor -->
            <span class="w-60 shrink-0 min-w-0 flex items-center gap-1.5">
              <span class="truncate text-gray-700 dark:text-gray-300" :title="log.user_email || undefined">
                {{ log.user_email || $t('settings.audit.system') }}
              </span>
              <span
                v-if="actorKind(log) === 'agent'"
                data-testid="audit-actor-agent"
                class="shrink-0 inline-flex items-center gap-0.5 px-1 py-px rounded text-[10px] bg-violet-50 text-violet-700 dark:bg-violet-950 dark:text-violet-300"
              >
                <UIcon name="i-heroicons-sparkles" class="w-2.5 h-2.5" />{{ $t('settings.audit.actorAgent') }}
              </span>
            </span>

            <!-- Action: muted resource path + coloured verb; truncates, never overlaps -->
            <span data-testid="audit-action" class="flex-1 min-w-0 flex items-center gap-1 truncate" :title="log.action">
              <span v-if="splitAction(log.action).path.length" class="truncate text-gray-400 dark:text-gray-500">
                {{ splitAction(log.action).path.join(' · ') }}
              </span>
              <span class="shrink-0 inline-flex px-1.5 py-0.5 rounded text-[10px] font-medium" :class="verbClass(log.action)">
                {{ splitAction(log.action).verb }}
              </span>
            </span>

            <!-- Target -->
            <span class="flex-1 min-w-0 truncate text-gray-500 dark:text-gray-400" :title="log.details?.title">
              <template v-if="log.resource_type">
                <span class="text-gray-400">{{ log.resource_type }}</span>
                <template v-if="log.details?.title"> · <span class="text-gray-700 dark:text-gray-300">{{ log.details.title }}</span></template>
              </template>
            </span>

            <UIcon name="i-heroicons-chevron-right" class="w-3.5 h-3.5 shrink-0 text-gray-300 group-hover:text-gray-500 rtl:-scale-x-100" />
          </div>
        </template>

        <!-- Empty State -->
        <div v-else class="py-8 text-center">
          <p class="text-xs text-gray-400 dark:text-gray-400">{{ $t('settings.audit.noActivity') }}</p>
        </div>
      </div>

      <!-- Pagination -->
      <div v-if="totalPages > 1" class="mt-2 flex items-center justify-between">
        <span class="text-[11px] text-gray-400 dark:text-gray-400">
          {{ $t('settings.audit.rangeCount', { start: (page - 1) * pageSize + 1, end: Math.min(page * pageSize, total), total }) }}
        </span>
        <div class="flex items-center gap-0.5">
          <button
            :disabled="page <= 1"
            class="px-1.5 py-0.5 text-[11px] text-gray-500 dark:text-gray-400 hover:text-gray-700 disabled:text-gray-300 disabled:cursor-not-allowed"
            @click="prevPage(buildFilters())"
          >
            {{ $t('settings.audit.prev') }}
          </button>
          <span class="px-1.5 text-[11px] text-gray-400 dark:text-gray-400">{{ page }}/{{ totalPages }}</span>
          <button
            :disabled="page >= totalPages"
            class="px-1.5 py-0.5 text-[11px] text-gray-500 dark:text-gray-400 hover:text-gray-700 disabled:text-gray-300 disabled:cursor-not-allowed"
            @click="nextPage(buildFilters())"
          >
            {{ $t('settings.audit.next') }}
          </button>
        </div>
      </div>

      <AuditLogDrawer :log="selected" @close="selected = null" />
    </template>
  </div>
</template>

<script setup lang="ts">
import { useAuditLogs, type AuditLogFilters } from '~/ee/composables/useAuditLogs'
import AuditFilterSelect from '~/components/audit/AuditFilterSelect.vue'
import AuditLogDrawer from '~/components/audit/AuditLogDrawer.vue'
import AuditStreams from '~/components/audit/AuditStreams.vue'
import { splitAction, verbClass, actorKind, type FilterOption } from '~/utils/auditActionFormat'
import type { AuditLog } from '~/ee/composables/useAuditLogs'

definePageMeta({
  auth: true,
  permissions: ['view_audit_logs'],
  layout: 'settings'
})

const route = useRoute()
const router = useRouter()
const { hasFeature, license } = useEnterprise()
const { logs, loading, error, total, page, pageSize, totalPages, fetchLogs, nextPage, prevPage, fetchActionTypes, fetchResourceTypes } = useAuditLogs()
const { t, locale } = useI18n({ useScope: 'global' })
const _df = useFormatDate()
const toast = useToast()
const { getErrorMessage } = useErrorMessage()

type Tab = 'activity' | 'streams'
type Range = '24h' | '7d' | '30d'
const RANGE_HOURS: Record<Range, number> = { '24h': 24, '7d': 24 * 7, '30d': 24 * 30 }

const q = route.query
const asStr = (v: unknown) => (typeof v === 'string' && v ? v : null)
const tab = ref<Tab>(q.tab === 'streams' ? 'streams' : 'activity')
const searchQuery = ref(asStr(q.search) || '')
const selectedActions = ref<string[]>(asStr(q.action)?.split(',').filter(Boolean) || [])
const selectedResource = ref<string | null>(asStr(q.resource_type))
const selectedUser = ref<string | null>(asStr(q.user_id))
const selectedRange = ref<string | null>(asStr(q.range) && asStr(q.range)! in RANGE_HOURS ? asStr(q.range) : null)
const selected = ref<AuditLog | null>(null)
const hasFetched = ref(false)
const showExport = ref(false)
const exporting = ref(false)
const exportRef = ref<HTMLElement | null>(null)

const streamsLicensed = computed(() => hasFeature('audit_log_streams'))

const actionTypes = ref<string[]>([])
const resourceTypes = ref<string[]>([])
const members = ref<Array<{ id: string; email: string; name?: string }>>([])

const actionOptions = computed<FilterOption[]>(() =>
  actionTypes.value.map((a) => {
    const { path, verb } = splitAction(a)
    return { value: a, label: path.length ? `${path.join(' · ')} · ${verb}` : verb, group: a.split('.')[0].replace(/_/g, ' ') }
  }),
)
const resourceOptions = computed<FilterOption[]>(() => resourceTypes.value.map((r) => ({ value: r, label: r })))
const userOptions = computed<FilterOption[]>(() => members.value.map((m) => ({ value: m.id, label: m.email, sub: m.name && m.name !== m.email ? m.name : undefined })))
const rangeOptions = computed<FilterOption[]>(() => (['24h', '7d', '30d'] as Range[]).map((r) => ({ value: r, label: t(`settings.audit.filters.range${r}`) })))

const hasActiveFilters = computed(() => !!(searchQuery.value || selectedActions.value.length || selectedResource.value || selectedUser.value || selectedRange.value))

const buildFilters = (): AuditLogFilters => {
  const range = selectedRange.value as Range | null
  return {
    action: selectedActions.value.length ? selectedActions.value.join(',') : undefined,
    resource_type: selectedResource.value || undefined,
    user_id: selectedUser.value || undefined,
    start_date: range ? new Date(Date.now() - RANGE_HOURS[range] * 3600_000).toISOString() : undefined,
    search: searchQuery.value || undefined,
  }
}

// Filter state lives in the URL so a filtered view can be shared or reloaded.
const syncUrl = () => {
  const query: Record<string, string> = {}
  if (tab.value === 'streams') query.tab = 'streams'
  if (searchQuery.value) query.search = searchQuery.value
  if (selectedActions.value.length) query.action = selectedActions.value.join(',')
  if (selectedResource.value) query.resource_type = selectedResource.value
  if (selectedUser.value) query.user_id = selectedUser.value
  if (selectedRange.value) query.range = selectedRange.value
  router.replace({ query })
}

const applyFilters = () => {
  page.value = 1
  syncUrl()
  fetchLogs(buildFilters())
}

watch([selectedActions, selectedResource, selectedUser, selectedRange], applyFilters, { deep: true })

let searchTimeout: ReturnType<typeof setTimeout> | null = null
const debouncedSearch = () => {
  if (searchTimeout) clearTimeout(searchTimeout)
  searchTimeout = setTimeout(applyFilters, 300)
}

const clearFilters = () => {
  searchQuery.value = ''
  selectedActions.value = []
  selectedResource.value = null
  selectedUser.value = null
  selectedRange.value = null
}

const setTab = (tb: Tab) => {
  tab.value = tb
  selected.value = null
  syncUrl()
}

const loadOptions = async () => {
  const { organization } = useOrganization()
  const [actions, resources] = await Promise.all([fetchActionTypes(), fetchResourceTypes()])
  actionTypes.value = actions
  resourceTypes.value = resources
  try {
    const res = await useMyFetch(`/organizations/${organization.value.id}/members`)
    const raw = (res.data.value as any[]) || []
    members.value = raw
      .map((m) => ({ id: m.user?.id || m.user_id, email: m.user?.email || m.email, name: m.user?.name }))
      .filter((m) => m.id && m.email)
  } catch {
    members.value = []
  }
}

const toDate = (s: string) => new Date(/[zZ]|[+-]\d\d:\d\d$/.test(s) ? s : s + 'Z')
const formatTimestamp = (timestamp: string) => {
  const d = toDate(timestamp)
  const sameDay = d.toDateString() === new Date().toDateString()
  return sameDay
    ? _df.format(d, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
    : _df.format(d, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })
}
const relativeTime = (timestamp: string) => {
  const diff = (toDate(timestamp).getTime() - Date.now()) / 1000
  const rtf = new Intl.RelativeTimeFormat(String(locale.value), { numeric: 'auto' })
  const abs = Math.abs(diff)
  if (abs < 60) return rtf.format(Math.round(diff), 'second')
  if (abs < 3600) return rtf.format(Math.round(diff / 60), 'minute')
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), 'hour')
  return rtf.format(Math.round(diff / 86400), 'day')
}

// Export streams the filtered view as a file; the request carries the auth
// and org headers, so it is fetched and saved as a blob rather than linked.
async function doExport(format: 'json' | 'csv') {
  showExport.value = false
  exporting.value = true
  try {
    const { token } = useAuth()
    const { organization } = useOrganization()
    const params = new URLSearchParams({ format })
    for (const [k, v] of Object.entries(buildFilters())) if (v) params.append(k, String(v))
    const resp = await fetch(`/api/enterprise/audit/export?${params.toString()}`, {
      headers: { Authorization: `${token.value}`, 'X-Organization-Id': organization.value.id },
    })
    if (!resp.ok) {
      let body: any = null
      try { body = await resp.json() } catch {}
      throw { data: body, statusCode: resp.status }
    }
    const blob = await resp.blob()
    const cd = resp.headers.get('content-disposition') || ''
    const name = /filename="([^"]+)"/.exec(cd)?.[1] || `audit-logs.${format === 'csv' ? 'csv' : 'jsonl'}`
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = name
    document.body.appendChild(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (e) {
    toast.add({ title: t('settings.audit.export.failed'), description: getErrorMessage(e), color: 'red' })
  } finally {
    exporting.value = false
  }
}

const onDocClick = (e: MouseEvent) => {
  if (exportRef.value && !exportRef.value.contains(e.target as Node)) showExport.value = false
}
onMounted(() => document.addEventListener('click', onDocClick))
onUnmounted(() => document.removeEventListener('click', onDocClick))

// Watch for license to load, then fetch logs
watch(
  () => license.value,
  (newLicense) => {
    if (newLicense && hasFeature('audit_logs') && !hasFetched.value) {
      hasFetched.value = true
      if (!streamsLicensed.value && tab.value === 'streams') tab.value = 'activity'
      loadOptions()
      fetchLogs(buildFilters())
    }
  },
  { immediate: true }
)
</script>
