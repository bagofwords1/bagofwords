<template>
  <div class="mb-3 rounded-lg border border-gray-200 bg-gray-50/70 px-3 py-2.5 dark:border-gray-700 dark:bg-gray-800/40" data-testid="schema-identity">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <div class="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs">
        <span v-if="showName" class="font-medium text-gray-700 dark:text-gray-200">{{ connection.name }}</span>
        <span class="text-gray-500">{{ t('schemaIdentity.viewing') }}</span>
        <button v-if="canSwitchIdentity" type="button" :disabled="busy || disabled" @click="detailsOpen = true"
          class="inline-flex items-center gap-1 font-medium text-gray-800 hover:text-blue-600 disabled:opacity-50 dark:text-gray-100"
          :title="t('schemaIdentity.changeIdentity')">
          {{ identityLabel }}<UIcon name="i-heroicons-chevron-down" class="h-3 w-3" />
        </button>
        <span v-else class="font-medium text-gray-800 dark:text-gray-100">{{ identityLabel }}</span>
      </div>
      <button v-if="showRefresh && canRefresh" type="button" :disabled="busy || disabled" @click="refreshSchema"
        class="inline-flex items-center gap-1.5 rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-[11px] text-gray-600 hover:bg-gray-50 disabled:opacity-50 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300">
        <UIcon name="i-heroicons-arrow-path" class="h-3 w-3" :class="{ 'animate-spin': busy }" />
        {{ t(busy ? 'schemaIdentity.refreshing' : 'schemaIdentity.refresh') }}
      </button>
    </div>
    <p class="mt-1 text-[11px] text-gray-500">{{ refreshedLabel }}</p>
    <p v-if="notice" role="status" class="mt-1.5 text-xs" :class="warning ? 'text-amber-700 dark:text-amber-400' : 'text-green-700 dark:text-green-400'">{{ notice }}</p>
    <ConnectionDetailModal v-model="detailsOpen" :connection="connection" @updated="emit('identity-changed')" />
  </div>
</template>

<script setup lang="ts">
import { isIndexingActive, type ConnectionIndexing } from '~/composables/useConnectionStatus'
import { useCan } from '~/composables/usePermissions'
import ConnectionDetailModal from '~/components/ConnectionDetailModal.vue'

const props = defineProps<{ connection: any; showName?: boolean; showRefresh?: boolean; disabled?: boolean }>()
const emit = defineEmits<{ (e: 'refreshed'): void; (e: 'identity-changed'): void }>()
const { t, locale } = useI18n()
const detailsOpen = ref(false)
const job = ref<ConnectionIndexing | null>(null)
const requesting = ref(false)
const requestFailed = ref(false)
const personal = computed(() => props.connection.user_status?.effective_auth === 'user')
const disconnected = computed(() => props.connection.auth_policy === 'user_required' && !['user', 'system'].includes(props.connection.user_status?.effective_auth))
const scope = computed(() => personal.value ? 'user' : 'org')
const identityLabel = computed(() => t(personal.value || disconnected.value ? 'schemaIdentity.myAccount' : 'schemaIdentity.serviceAccount'))
const canSwitchIdentity = computed(() => props.connection.user_status?.can_switch_identity && useCan('manage_connection', { type: 'connection', id: props.connection.id }))
const canRefresh = computed(() => !disconnected.value && (personal.value || useCan('manage_connection', { type: 'connection', id: props.connection.id })))
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
let generation = 0
let timer: ReturnType<typeof setTimeout> | undefined
async function readJob(version: number) {
  const { data, error } = await useMyFetch(`/connections/${props.connection.id}/indexing?scope=${scope.value}`, { method: 'GET' })
  if (version !== generation) return
  if (error.value) {
    if (isIndexingActive(job.value)) { requestFailed.value = true; job.value = null }
    return
  }
  const wasActive = isIndexingActive(job.value)
  job.value = data.value as ConnectionIndexing
  if (isIndexingActive(job.value)) timer = setTimeout(() => readJob(version), 2000)
  else if (wasActive) emit('refreshed')
}
async function refreshSchema() {
  if (!canRefresh.value || busy.value || props.disabled) return
  requesting.value = true
  requestFailed.value = false
  const version = generation
  try {
    const endpoint = personal.value ? 'my-schema/refresh?background=true' : 'refresh'
    const { data, error } = await useMyFetch(`/connections/${props.connection.id}/${endpoint}`, { method: 'POST' })
    if (version !== generation) return
    if (error.value || !(data.value as any)?.indexing) { requestFailed.value = true; return }
    job.value = (data.value as any).indexing
    if (isIndexingActive(job.value)) await readJob(version)
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
  if (!disconnected.value) readJob(version)
}, { immediate: true })
onBeforeUnmount(() => { generation++; clearTimeout(timer) })
</script>
