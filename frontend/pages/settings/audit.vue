<template>
  <div>
    <!-- Activity / Streams switch sits in the page header (settings shell). -->
    <Teleport to="#settings-page-actions" defer>
      <div v-if="hasFeature('audit_logs') && streamsLicensed" class="inline-flex rounded-lg bg-surface-muted p-0.5 text-sm" role="tablist">
        <button
          v-for="tb in (['activity', 'streams'] as const)"
          :key="tb"
          type="button"
          role="tab"
          :aria-selected="tab === tb"
          :data-testid="`audit-tab-${tb}`"
          class="px-3 py-1 rounded-md font-medium transition-colors"
          :class="tab === tb ? 'bg-surface-elevated text-ink shadow-sm' : 'text-ink-muted hover:text-ink'"
          @click="setTab(tb)"
        >{{ $t(`settings.audit.tabs.${tb}`) }}</button>
      </div>
    </Teleport>

    <!-- Enterprise Gate -->
    <div v-if="!hasFeature('audit_logs')" class="rounded-lg border border-dashed border-line">
      <SettingsEmpty icon="i-heroicons-clipboard-document-list" :title="$t('settings.audit.enterpriseRequired')">
        <UButton to="https://docs.bagofwords.com/enterprise" target="_blank" color="gray" trailing-icon="i-heroicons-arrow-up-right">
          {{ $t('settings.audit.learnMore') }}
        </UButton>
      </SettingsEmpty>
    </div>

    <AuditStreams v-else-if="tab === 'streams' && streamsLicensed" />

    <!-- Activity -->
    <template v-else>
      <div class="mb-4 flex items-center gap-2 flex-wrap">
        <UInput
          v-model="searchQuery"
          icon="i-heroicons-magnifying-glass"
          data-testid="audit-search"
          :placeholder="$t('settings.audit.searchPlaceholder')"
          class="w-full sm:w-64"
          @input="debouncedSearch"
        />

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

        <UButton
          v-if="hasActiveFilters"
          color="gray"
          variant="ghost"
          data-testid="audit-clear"
          @click="clearFilters"
        >
          {{ $t('settings.audit.clear') }}
        </UButton>

        <div ref="exportRef" class="relative ms-auto">
          <UButton
            color="gray"
            data-testid="audit-export"
            :icon="exporting ? 'i-heroicons-arrow-path' : 'i-heroicons-arrow-down-tray'"
            :loading="exporting"
            :disabled="exporting"
            @click="showExport = !showExport"
          >
            {{ $t('settings.audit.export.button') }}
          </UButton>
          <div v-if="showExport" class="absolute top-full end-0 mt-1 w-64 bg-surface-elevated border border-line rounded-lg shadow-lg z-dropdown py-1">
            <button type="button" data-testid="audit-export-json" class="w-full text-start px-3 py-2 text-sm text-ink-2 hover:bg-ink/[0.03]" @click="doExport('json')">{{ $t('settings.audit.export.json') }}</button>
            <button type="button" data-testid="audit-export-csv" class="w-full text-start px-3 py-2 text-sm text-ink-2 hover:bg-ink/[0.03]" @click="doExport('csv')">{{ $t('settings.audit.export.csv') }}</button>
            <p class="px-3 pt-2 pb-1.5 text-xs text-ink-muted border-t border-line mt-1">{{ $t('settings.audit.export.hint') }}</p>
          </div>
        </div>
      </div>

      <SettingsLoading v-if="loading && !logs.length" />

      <div v-else-if="error" class="py-6 text-center text-sm text-red-600 dark:text-red-400">{{ error }}</div>

      <!-- Logs list -->
      <div v-else class="bow-table-card divide-y divide-line-subtle transition-opacity" :class="loading ? 'opacity-60' : ''">
        <template v-if="logs.length > 0">
          <div
            v-for="log in logs"
            :key="log.id"
            data-testid="audit-row"
            role="button"
            tabindex="0"
            class="group flex items-center gap-4 px-5 py-3 text-sm cursor-pointer transition-colors hover:bg-ink/[0.03] focus:outline-none focus-visible:bg-ink/[0.03]"
            :class="selected?.id === log.id ? 'bg-blue-50/60 dark:bg-blue-950/40' : ''"
            @click="selected = log"
            @keydown.enter.prevent="selected = log"
          >
            <!-- Time -->
            <span class="w-20 shrink-0 text-xs text-ink-muted tabular-nums" :title="relativeTime(log.created_at)">
              {{ formatTimestamp(log.created_at) }}
            </span>

            <!-- Actor -->
            <span class="w-56 shrink-0 min-w-0 flex items-center gap-1.5">
              <span class="truncate text-ink" :title="log.user_email || undefined">
                {{ log.user_email || $t('settings.audit.system') }}
              </span>
              <span
                v-if="actorKind(log) === 'agent'"
                data-testid="audit-actor-agent"
                class="shrink-0 inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-xs bg-violet-50 text-violet-700 dark:bg-violet-950 dark:text-violet-300"
              >
                <UIcon name="i-heroicons-sparkles" class="w-3 h-3" />{{ $t('settings.audit.actorAgent') }}
              </span>
            </span>

            <!-- Action: muted resource path + coloured verb; truncates, never overlaps -->
            <span data-testid="audit-action" class="flex-1 min-w-0 flex items-center gap-1.5 truncate" :title="log.action">
              <span v-if="splitAction(log.action).path.length" class="truncate text-ink-muted">
                {{ splitAction(log.action).path.join(' · ') }}
              </span>
              <span class="shrink-0 inline-flex px-1.5 py-0.5 rounded text-xs font-medium" :class="verbClass(log.action)">
                {{ splitAction(log.action).verb }}
              </span>
            </span>

            <!-- Target -->
            <span class="flex-1 min-w-0 truncate text-ink-muted" :title="log.details?.title">
              <template v-if="log.resource_type">
                <span>{{ log.resource_type }}</span>
                <template v-if="log.details?.title"> · <span class="text-ink-2">{{ log.details.title }}</span></template>
              </template>
            </span>

            <UIcon name="i-heroicons-chevron-right" class="w-4 h-4 shrink-0 text-ink-subtle group-hover:text-ink-muted transition-colors" />
          </div>
        </template>

        <SettingsEmpty v-else icon="i-heroicons-clipboard-document-list" :title="$t('settings.audit.noActivity')" />
      </div>

      <!-- Pagination -->
      <div v-if="totalPages > 1" class="mt-3 flex items-center justify-between">
        <span class="text-xs text-ink-muted tabular-nums">
          {{ $t('settings.audit.rangeCount', { start: (page - 1) * pageSize + 1, end: Math.min(page * pageSize, total), total }) }}
        </span>
        <div class="flex items-center gap-1">
          <UButton color="gray" variant="ghost" size="xs" icon="i-heroicons-chevron-left" :disabled="page <= 1" @click="prevPage(buildFilters())">
            {{ $t('settings.audit.prev') }}
          </UButton>
          <span class="px-1.5 text-xs text-ink-muted tabular-nums">{{ page }} / {{ totalPages }}</span>
          <UButton color="gray" variant="ghost" size="xs" trailing-icon="i-heroicons-chevron-right" :disabled="page >= totalPages" @click="nextPage(buildFilters())">
            {{ $t('settings.audit.next') }}
          </UButton>
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
import SettingsEmpty from '~/components/settings/SettingsEmpty.vue'
import SettingsLoading from '~/components/settings/SettingsLoading.vue'
import { splitAction, verbClass, actorKind, type FilterOption } from '~/utils/auditActionFormat'
import type { AuditLog } from '~/ee/composables/useAuditLogs'

definePageMeta({
  auth: true,
  permissions: ['view_audit_logs']
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
