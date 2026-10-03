<template>
  <UModal v-model="open" :ui="{ width: 'sm:max-w-4xl' }">
    <div class="p-5 flex flex-col max-h-[calc(100dvh-5rem)]">
      <div class="flex items-center gap-2 mb-4 shrink-0">
        <DataSourceIcon :type="connection?.type" :connector-key="connection?.connector_key" class="h-5" />
        <h3 class="text-lg font-semibold">{{ $t('data.manageConnection') }}</h3>
        <button :aria-label="$t('common.close')" class="ms-auto text-gray-400 hover:text-gray-600" @click="open = false"><UIcon name="heroicons-x-mark" class="w-5 h-5" /></button>
      </div>
      <div class="md:grid min-h-0 overflow-y-auto md:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)] -mx-5 border-t border-gray-100 dark:border-gray-800">
        <div class="min-w-0 px-6 py-6 md:px-8 md:max-h-[70vh] md:overflow-y-auto">
          <div class="flex items-center justify-between gap-3 mb-6"><h4 class="text-sm font-medium">{{ $t('data.connectionSettings') }}</h4><button v-if="!editing" :disabled="loading || state.busy" class="text-xs text-blue-600" @click="editing = true">{{ $t('data.editSettings') }}</button></div>
          <Spinner v-if="loading" class="w-5 h-5 mx-auto" />
          <fieldset v-else :disabled="state.busy">
            <ConnectForm v-if="open && localValues" :key="formRevision" ref="form" :read-only="!editing" unified-edit embedded mode="edit" :initial-type="connection.type" :connection-id="connection.id" :initial-values="localValues" :force-show-system-credentials="true" :show-require-user-auth-toggle="true" :show-l-l-m-toggle="false" :allow-name-edit="true" :hide-header="true" @state="updateState" @success="saved" />
          </fieldset>
        </div>
        <aside class="connection-status min-w-0 border-t md:border-t-0 md:border-s border-gray-100 dark:border-gray-800 px-6 py-6 md:px-8 md:max-h-[70vh] md:overflow-y-auto" aria-live="polite">
          <h4 class="status-heading font-medium">{{ $t('data.setupStatus') }}</h4>
          <div class="mt-2 mb-5" data-testid="management-account">
            <p class="status-meta font-medium text-gray-700 dark:text-gray-300">{{ $t(personalManagement ? 'data.yourAccount' : 'data.organizationAccount') }}</p>
            <p class="status-meta text-gray-500 mt-1">{{ $t(personalManagement ? 'data.managementPersonalHint' : 'data.managementOrganizationHint') }}</p>
            <button v-if="needsPersonalSignIn" :disabled="signingIn" class="mt-2 inline-flex items-center gap-1.5 rounded bg-blue-500 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50" @click="signIn"><Spinner v-if="signingIn" class="h-3 w-3" />{{ $t('data.signIn') }}</button>
          </div>
          <div v-if="!needsPersonalSignIn" class="space-y-1 mb-6">
            <div class="flex items-start gap-2 status-body text-gray-800 dark:text-gray-200">
              <Spinner v-if="state.busy" class="w-4 h-4 shrink-0 mt-0.5 text-gray-400" data-testid="connection-testing-spinner" />
              <UIcon v-else :name="testIsError ? 'heroicons-exclamation-circle' : testWarning ? 'heroicons-exclamation-triangle' : lastResult?.success ? 'heroicons-check-circle' : 'heroicons-minus-circle'" class="w-4 h-4 shrink-0 mt-0.5" :class="testIsError ? 'text-red-600' : testWarning ? 'text-amber-600' : lastResult?.success ? 'text-green-600' : 'text-gray-400'" />
              <p role="status" class="whitespace-pre-wrap break-words" :class="testIsError ? 'text-red-600' : testWarning ? 'text-amber-700 dark:text-amber-400' : ''">{{ state.busy ? $t('data.testing') : state.error || state.warning || (lastResult?.success && !testWarning ? $t('data.lastConnectionTestPassed') : state.message || lastResult?.message || $t('data.never')) }}</p>
            </div>
            <p class="status-meta text-gray-500 ps-6">{{ $t('data.lastChecked') }} · {{ checkedAt ? new Date(checkedAt).toLocaleString(locale) : $t('data.never') }}</p>
            <p v-if="state.settingsChanged" class="status-meta text-amber-700 dark:text-amber-400 ps-6">{{ $t('data.settingsNeedTest') }}</p>
          </div>
          <section class="space-y-2">
            <p class="status-meta text-gray-500">{{ $t(personalManagement ? 'data.myAccount' : 'data.sharedSchema') }}</p>
            <ConnectionIndexingProgress v-if="indexing" :indexing="indexing" :show-logs="true" compact />
            <p v-if="operationError" role="alert" class="status-body text-red-600 whitespace-pre-wrap break-words">{{ operationError }}</p>
          </section>
          <div class="flex flex-wrap items-center gap-2 my-4" data-testid="connection-actions">
            <button :disabled="loading || state.busy || needsPersonalSignIn" class="status-body border border-gray-200 dark:border-gray-700 rounded px-3 py-1.5 disabled:opacity-50" @click="form?.test()">{{ $t('data.testConnection') }}</button>
            <button :disabled="refreshing || active || needsPersonalSignIn" class="status-body border border-gray-200 dark:border-gray-700 rounded px-3 py-1.5 disabled:opacity-50" @click="refreshSchema">{{ active || refreshing ? $t('data.refreshing') : $t(personalManagement ? 'data.refreshMySchema' : 'data.refreshSharedSchema') }}</button>
          </div>
          <ConnectionScheduleSettings v-if="!personalManagement" :connection="connection" :displayed-error="indexing?.error" />
          <ConnectionDeleteAction :connection="connection" compact @deleted="open = false; emit('deleted')" />
        </aside>
      </div>
      <div class="flex shrink-0 justify-end gap-3 border-t border-gray-100 dark:border-gray-800 pt-4">
        <template v-if="editing"><button :disabled="state.busy" class="text-xs px-3 py-1.5 text-gray-600 dark:text-gray-300" @click="cancelEdit">{{ $t('data.cancel') }}</button><button :disabled="loading || state.busy" class="bg-blue-500 hover:bg-blue-600 text-white text-xs font-medium py-1.5 px-3 rounded disabled:opacity-50" @click="form?.connect()">{{ state.busy ? $t('data.saving') : $t('common.saveChanges') }}</button></template>
        <button v-else class="bg-blue-500 hover:bg-blue-600 text-white text-xs font-medium py-1.5 px-3 rounded" @click="open = false">{{ $t('data.setupDone') }}</button>
      </div>
    </div>
  </UModal>
</template>
<script setup lang="ts">
import ConnectForm from '~/components/datasources/ConnectForm.vue'
import type { ConnectionIndexing } from '~/composables/useConnectionStatus'
import { connectionSignInError } from '~/composables/useConnectionSignIn'
const props = defineProps<{ modelValue: boolean; connection: any; initialValues: any; loading?: boolean; lastTest?: { success: boolean; message: string } | null }>()
const emit = defineEmits(['update:modelValue', 'success', 'deleted', 'tested'])
const open = computed({ get: () => props.modelValue, set: value => emit('update:modelValue', value) })
const { t, locale } = useI18n()
const editing = ref(false)
const formRevision = ref(0)
const savedValues = ref<any>(null)
const localValues = computed(() => savedValues.value || props.initialValues)
const personalManagement = computed(() => localValues.value?.management_auth === 'user')
const needsPersonalSignIn = computed(() => personalManagement.value && props.connection?.allowed_user_auth_modes?.includes('oauth') && !props.connection?.user_status?.has_user_credentials)
const signingIn = ref(false)
const route = useRoute()
async function signIn() {
  signingIn.value = true
  try {
    const { data, error } = await useMyFetch(`/connections/${props.connection.id}/oauth/authorize?return_to=${encodeURIComponent(route.fullPath)}`, { method: 'GET' })
    if (error.value || !(data.value as any)?.authorization_url) {
      operationError.value = error.value
        ? connectionSignInError(error.value, t('data.requestFailed'))
        : t('data.requestFailed')
      signingIn.value = false
      return
    }
    window.location.href = (data.value as any).authorization_url
  } catch (e: any) { operationError.value = connectionSignInError(e, t('data.requestFailed')); signingIn.value = false }
}
function cancelEdit() { editing.value = false; formRevision.value++; state.value = { busy: false, error: '', warning: '', message: '', settingsChanged: false } }
async function saved(result: any) {
  const { data } = await useMyFetch(`/connections/${props.connection.id}`, { method: 'GET' })
  if (data.value) { const d = data.value as any; savedValues.value = { ...d, credentials: d.credentials_meta || {} } }
  cancelEdit()
  emit('success', result)
}
const form = ref<InstanceType<typeof ConnectForm> | null>(null)
const state = ref({ busy: false, error: '', warning: '', message: '', settingsChanged: false })
const indexing = ref<ConnectionIndexing | null>(null)
const currentTest = ref<{ success: boolean; message: string; warning?: boolean } | null>(null)
const testedAt = ref<string | null>(null)
function updateState(value: typeof state.value) {
  const finished = state.value.busy && !value.busy
  state.value = value
  if (finished) emit('tested')
  const message = value.error || value.warning || value.message
  if (!value.busy && message) {
    currentTest.value = { success: !value.error, message, warning: !!value.warning }
    testedAt.value = new Date().toISOString()
  }
}
const testWarning = computed(() => !!state.value.warning || currentTest.value?.warning)
const testIsError = computed(() => !!state.value.error || (!state.value.message && !state.value.warning && lastResult.value?.success === false))
const checkedAt = computed(() => testedAt.value || (!personalManagement.value && localValues.value?.last_connection_checked_at))
const lastResult = computed(() => {
  if (currentTest.value) return currentTest.value
  const status = !personalManagement.value && localValues.value?.last_connection_status
  if (!status) return null
  const success = status === 'success'
  return { success, message: t(success ? 'data.connectionSuccessful' : 'data.connectionFailed') }
})
const active = computed(() => ['pending', 'running'].includes(indexing.value?.status || ''))
const refreshing = ref(false)
const operationError = ref('')
let session = 0
let timer: ReturnType<typeof setTimeout> | undefined
function stop() { session++; clearTimeout(timer) }
async function poll(current = session) {
  const id = props.connection?.id
  if (!id || !open.value) return
  try {
    const { data } = await useMyFetch(`/connections/${id}/indexing?scope=${personalManagement.value ? 'user' : 'org'}`, { method: 'GET' })
    if (current !== session) return
    if (data.value) indexing.value = data.value as ConnectionIndexing
  } catch {
    // Keep the last result while retrying a transient polling failure.
  } finally {
    if (current === session && active.value) timer = setTimeout(() => poll(current), 1500)
  }
}
async function refreshSchema() {
  if (refreshing.value || active.value) return
  refreshing.value = true
  operationError.value = ''
  const current = session
  try {
    const action = personalManagement.value ? 'my-schema/refresh?background=true' : 'reindex'
    const { data, error } = await useMyFetch(`/connections/${props.connection.id}/${action}`, { method: 'POST' })
    if (current !== session) return
    if (error.value) { operationError.value = (error.value as any)?.data?.detail || t('data.reindexFailed'); return }
    indexing.value = (data.value as any)?.indexing || null
    clearTimeout(timer)
    if (active.value) poll(current)
  } finally { if (current === session) refreshing.value = false }
}
watch(() => [props.modelValue, props.connection?.id], () => {
  stop()
  if (!props.modelValue || !props.connection?.id) return
  editing.value = false
  savedValues.value = null
  formRevision.value++
  currentTest.value = null
  testedAt.value = null
  operationError.value = ''
  refreshing.value = false
  signingIn.value = false
  state.value = { busy: false, error: '', warning: '', message: '', settingsChanged: false }
  const scope = personalManagement.value ? 'user' : 'org'
  indexing.value = props.connection?.indexing?.scope === scope ? props.connection.indexing : null
  poll()
}, { immediate: true })
watch(personalManagement, () => {
  stop()
  indexing.value = null
  currentTest.value = null
  testedAt.value = null
  if (open.value) poll()
})
onBeforeUnmount(stop)
</script>

<style scoped>
.connection-status { --status-body: 13px; --status-meta: 12px; font-size: var(--status-body); line-height: 1.5; font-weight: 400; }
.connection-status :deep(.status-body),
.connection-status :deep(.text-sm),
.connection-status :deep(.text-\[13px\]) { font-size: var(--status-body); line-height: 1.5; }
.connection-status :deep(.status-meta),
.connection-status :deep(.text-xs),
.connection-status :deep(.text-\[11px\]) { font-size: var(--status-meta); line-height: 1.5; }
.connection-status .status-heading { font-size: 14px; line-height: 1.5; }
.connection-status :deep(summary) { font-size: var(--status-body); font-weight: 400; }
</style>
