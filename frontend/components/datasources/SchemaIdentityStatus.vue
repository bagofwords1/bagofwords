<template>
  <div class="inline-flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs" data-testid="schema-identity">
    <DataSourceIcon :type="connection.type" class="h-4 w-4" />
    <span class="font-medium text-gray-700 dark:text-gray-200">{{ connection.name }}</span>
    <button v-if="canSwitchIdentity" type="button" :disabled="busy || disabled" @click="detailsOpen = true"
      class="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium transition-colors disabled:opacity-50"
      :class="identityPillClass" :title="t('schemaIdentity.changeIdentity')">
      {{ identityLabel }}<UIcon name="i-heroicons-chevron-down" class="h-3 w-3" />
    </button>
    <span v-else class="rounded-full border px-2 py-0.5 text-[11px] font-medium" :class="identityPillClass">{{ identityLabel }}</span>
    <span v-if="displayScope !== scope" class="text-[11px] text-gray-500">{{ displayedScopeLabel }}</span>
    <span class="inline-flex items-center gap-1 text-[11px] text-gray-500" :title="notice || refreshedLabel">
      <span class="h-1.5 w-1.5 rounded-full" :class="busy ? 'bg-blue-500' : warning ? 'bg-amber-500' : job?.status === 'completed' ? 'bg-green-500' : 'bg-gray-400'" />
      <time v-if="timestamp" :datetime="job?.finished_at || undefined">{{ timestamp }}</time>
      <span v-else>—</span>
    </span>
    <span v-if="disconnected || (hasRequested && notice)" role="status" class="max-w-64 truncate text-[11px]"
      :class="warning ? 'text-amber-700 dark:text-amber-400' : 'text-green-700 dark:text-green-400'" :title="notice">{{ notice }}</span>
    <ConnectionDetailModal v-model="detailsOpen" :connection="connection" @updated="emit('identity-changed')" />
  </div>
</template>

<script setup lang="ts">
import { isIndexingActive, type ConnectionIndexing } from '~/composables/useConnectionStatus'
import { useCan } from '~/composables/usePermissions'
import ConnectionDetailModal from '~/components/ConnectionDetailModal.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'

const props = defineProps<{ connection: any; disabled?: boolean }>()
const emit = defineEmits<{ (e: 'refreshed'): void; (e: 'identity-changed'): void; (e: 'busy-change', active: boolean): void }>()
const { t, locale } = useI18n()
const detailsOpen = ref(false)
const job = ref<ConnectionIndexing | null>(null)
const requesting = ref(false)
const requestFailed = ref(false)
const hasRequested = ref(false)
const personal = computed(() => props.connection.user_status?.effective_auth === 'user')
const disconnected = computed(() => props.connection.auth_policy === 'user_required' && !['user', 'system'].includes(props.connection.user_status?.effective_auth))
const scope = computed<'user' | 'org'>(() => personal.value ? 'user' : 'org')
const displayScope = ref<'user' | 'org'>('org')
const identityLabel = computed(() => t(props.connection.auth_policy !== 'user_required'
  ? 'schemaIdentity.sharedConnection'
  : personal.value || disconnected.value ? 'schemaIdentity.myAccount' : 'schemaIdentity.serviceAccount'))
const identityPillClass = computed(() => props.connection.auth_policy !== 'user_required'
  ? 'border-gray-200 bg-gray-50 text-gray-600 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300'
  : personal.value || disconnected.value
    ? 'border-blue-200 bg-blue-50 text-blue-700 hover:bg-blue-100 dark:border-blue-800 dark:bg-blue-950 dark:text-blue-300'
    : 'border-violet-200 bg-violet-50 text-violet-700 hover:bg-violet-100 dark:border-violet-800 dark:bg-violet-950 dark:text-violet-300')
const displayedScopeLabel = computed(() => t(props.connection.auth_policy !== 'user_required'
  ? 'schemaIdentity.sharedConnection'
  : displayScope.value === 'user' ? 'schemaIdentity.myAccount' : 'schemaIdentity.sharedSchema'))
const canSwitchIdentity = computed(() => props.connection.user_status?.can_switch_identity && useCan('manage_connection', { type: 'connection', id: props.connection.id }))
const busy = computed(() => requesting.value || isIndexingActive(job.value))
const partial = computed(() => !!job.value?.stats?.unreadable_dataset_count || !!job.value?.stats?.unreadable_datasets?.length || (job.value?.events || []).some(e => ['warn', 'warning', 'error'].includes(e.level)))
const warning = computed(() => requestFailed.value || partial.value || ['failed', 'cancelled'].includes(job.value?.status || '') || disconnected.value)
const notice = computed(() => {
  if (disconnected.value) return t('schemaIdentity.connectRequired')
  if (requestFailed.value || ['failed', 'cancelled'].includes(job.value?.status || '')) return t('schemaIdentity.failed')
  if (job.value?.status !== 'completed') return ''
  return t(partial.value ? 'schemaIdentity.partial' : 'schemaIdentity.updated')
})
const refreshedLabel = computed(() => {
  // A credential's last_used_at is not a schema refresh timestamp.
  const value = job.value?.status === 'completed' ? job.value.finished_at : null
  if (!value) return t('schemaIdentity.unknown')
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return t('schemaIdentity.unknown')
  return t(partial.value ? 'schemaIdentity.attemptedAt' : 'schemaIdentity.refreshedAt', {
    time: new Intl.DateTimeFormat(locale.value, { dateStyle: 'medium', timeStyle: 'short' }).format(date),
  })
})
const timestamp = computed(() => {
  const value = job.value?.status === 'completed' ? job.value.finished_at : null
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return new Intl.DateTimeFormat(locale.value, {
    month: 'short', day: 'numeric',
    ...(date.getFullYear() === new Date().getFullYear() ? {} : { year: 'numeric' as const }),
  }).format(date)
})
watch([hasRequested, busy], () => emit('busy-change', hasRequested.value && busy.value), { immediate: true })
let generation = 0
let timer: ReturnType<typeof setTimeout> | undefined
async function readJob(version: number, selectedScope: 'user' | 'org') {
  const { data, error } = await useMyFetch(`/connections/${props.connection.id}/indexing?scope=${selectedScope}`, { method: 'GET' })
  if (version !== generation) return
  if (error.value) {
    if (isIndexingActive(job.value)) { requestFailed.value = true; job.value = null }
    return
  }
  const wasActive = isIndexingActive(job.value)
  job.value = data.value as ConnectionIndexing
  if (isIndexingActive(job.value)) timer = setTimeout(() => readJob(version, selectedScope), 2000)
  else if (wasActive) emit('refreshed')
}
async function refreshSchema(selectedScope: 'user' | 'org') {
  const canRefresh = selectedScope === 'user'
    ? personal.value
    : props.connection.catalog_ownership !== 'per_user'
      && useCan('manage_connection', { type: 'connection', id: props.connection.id })
  if (!canRefresh || disconnected.value || busy.value || props.disabled) return
  clearTimeout(timer)
  const version = ++generation
  displayScope.value = selectedScope
  hasRequested.value = true
  requesting.value = true
  requestFailed.value = false
  try {
    const endpoint = selectedScope === 'user' ? 'my-schema/refresh?background=true' : 'refresh'
    const { data, error } = await useMyFetch(`/connections/${props.connection.id}/${endpoint}`, { method: 'POST' })
    if (version !== generation) return
    if (error.value || !(data.value as any)?.indexing) { requestFailed.value = true; return }
    job.value = (data.value as any).indexing
    if (isIndexingActive(job.value)) await readJob(version, selectedScope)
    else emit('refreshed')
  } catch {
    if (version === generation) requestFailed.value = true
  } finally { requesting.value = false }
}
watch(() => [props.connection.id, scope.value, disconnected.value], () => {
  const version = ++generation
  clearTimeout(timer)
  job.value = null
  requestFailed.value = false
  hasRequested.value = false
  displayScope.value = scope.value
  if (!disconnected.value) readJob(version, scope.value)
}, { immediate: true })
onBeforeUnmount(() => { generation++; clearTimeout(timer); emit('busy-change', false) })
defineExpose({ refreshSchema })
</script>
