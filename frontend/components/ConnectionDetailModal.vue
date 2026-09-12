<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-md' }">
    <div class="connection-access p-6 max-h-[calc(100dvh-4rem)] overflow-y-auto">
      <header class="flex items-start gap-3">
        <DataSourceIcon :type="connection?.type" :connector-key="connection?.connector_key" class="h-6 shrink-0" />
        <div class="min-w-0 flex-1">
          <h2 class="font-semibold text-gray-900 dark:text-white break-words">{{ connection?.name }}</h2>
          <div v-if="headerStatus" class="access-meta flex flex-wrap items-center gap-x-1.5 mt-1 text-gray-500" role="status">
            <Spinner v-if="headerStatus === 'indexing'" class="w-3 h-3" />
            <span v-else class="w-1.5 h-1.5 rounded-full" :class="statusDotClass(headerStatus)" />
            <span>{{ $t(statusLabelKey(headerStatus)) }}</span>
            <span v-if="headerCheckedDisplay" class="inline-flex items-center gap-1.5" data-testid="connection-last-checked"><span aria-hidden="true">·</span><span>{{ $t('data.lastCheckedRelative', { time: headerCheckedDisplay }) }}</span></span>
          </div>
        </div>
        <button :aria-label="$t('common.close')" @click="isOpen = false" class="-me-2 -mt-2 p-2 rounded-md text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800"><UIcon name="heroicons-x-mark" class="w-4 h-4" /></button>
      </header>
      <p v-if="headerStatus === 'error' && !canUpdateDataSource" class="access-meta mt-3 text-gray-600 dark:text-gray-400">{{ $t('data.contactConnectionAdmin') }}</p>
      <div class="access-meta mt-3 flex items-start gap-2" data-testid="connection-used-by">
        <span class="shrink-0 text-gray-500">{{ $t('data.usedBy') }}</span>
        <Spinner v-if="loadingAgents" class="w-3 h-3 mt-0.5 text-gray-400" />
        <span v-else-if="agentsError" class="text-gray-500">{{ $t('data.requestFailed') }}</span>
        <span v-else-if="!accessibleAgents.length" class="text-gray-500">{{ $t('data.noAccessibleAgents') }}</span>
        <span v-else class="min-w-0 text-gray-700 dark:text-gray-300">
          <template v-for="(agent, i) in accessibleAgents" :key="agent.id"><span v-if="i">, </span><span class="inline-flex items-center gap-1 align-middle"><DataSourceIcon :type="connection?.type" :connector-key="connection?.connector_key" :icon="agent.icon" class="w-3.5 h-3.5 shrink-0" /><span class="break-words">{{ agent.name }}</span></span></template>
        </span>
      </div>

      <section v-if="requiresUserAuth" data-testid="query-access" class="mt-5 pt-5 border-t border-gray-100 dark:border-gray-800">
        <fieldset :disabled="switchingIdentity" :aria-label="$t('data.accessDataUsing')">
          <legend v-if="canChooseIdentity" class="mb-3 font-medium text-gray-900 dark:text-gray-100">{{ $t('data.accessDataUsing') }}</legend>
          <div class="space-y-4">
            <div v-for="option in visibleIdentityOptions" :key="option.value" :data-testid="option.value === 'self' ? 'personal-account' : 'organization-account'">
              <component :is="canChooseIdentity ? 'label' : 'div'" class="flex items-start gap-3" :class="{ 'cursor-pointer': canChooseIdentity }">
                <input v-if="canChooseIdentity" type="radio" name="connection-query-identity" :value="option.value" :checked="queryIdentity === option.value" @change="setIdentity(option.value)" class="mt-0.5 h-4 w-4 shrink-0 accent-blue-500" />
                <span class="min-w-0">
                  <span class="block text-gray-900 dark:text-gray-100">{{ option.label }}</span>
                  <span class="access-meta block mt-0.5 text-gray-500">{{ option.description }}</span>
                </span>
              </component>
              <div v-if="option.value === 'self' && personalAccess" :class="{ 'ms-7': canChooseIdentity }">
                <div v-if="!needsSignIn" class="access-meta flex flex-wrap mt-1.5 items-center gap-1.5 text-gray-500" role="status" data-testid="personal-access-status">
                  <Spinner v-if="accessStatus === 'indexing'" class="w-3 h-3" />
                  <span v-else class="w-1.5 h-1.5 rounded-full" :class="statusDotClass(accessStatus)" />
                  <span>{{ $t(statusLabelKey(accessStatus)) }}</span>
                  <span v-if="personalCheckedDisplay" class="inline-flex items-center gap-1.5" data-testid="connection-last-checked"><span aria-hidden="true">·</span><span>{{ $t('data.lastCheckedRelative', { time: personalCheckedDisplay }) }}</span></span>
                </div>
                <div class="flex flex-wrap items-center gap-3 mt-3">
                  <button v-if="needsSignIn" @click="openCredentialsModal" :disabled="connecting || switchingIdentity" class="access-button bg-blue-500 text-white hover:bg-blue-600"><Spinner v-if="connecting" class="w-3.5 h-3.5" />{{ $t('data.signIn') }}</button>
                  <template v-else>
                    <button @click="reloadMySchema" :disabled="reloadingMySchema || isIndexingActive(indexingState) || switchingIdentity" class="access-button border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800"><Spinner v-if="reloadingMySchema || isIndexingActive(indexingState)" class="w-3.5 h-3.5" />{{ reloadingMySchema || isIndexingActive(indexingState) ? $t('data.refreshing') : $t('data.refreshAccess') }}</button>
                    <button @click="disconnect" :disabled="disconnecting || switchingIdentity" class="access-meta font-medium text-gray-500 hover:text-red-600 disabled:opacity-50">{{ disconnecting ? $t('data.disconnecting') : $t('data.signOut') }}</button>
                  </template>
                </div>
              </div>
            </div>
          </div>
        </fieldset>
        <p v-if="personalRefreshError" role="alert" class="access-meta mt-3 text-red-600">{{ personalRefreshError }}</p>
        <ConnectionIndexingProgress v-if="personalAccess && indexingState && indexingState.status !== 'completed'" class="mt-3" :indexing="indexingState" compact />
        <p v-for="(problem, i) in personalWarnings" :key="i" class="access-meta mt-3 text-amber-700 whitespace-pre-wrap break-words" role="status">{{ problem.message }}</p>
      </section>

      <footer v-if="canUpdateDataSource" class="flex mt-5 pt-4 border-t border-gray-100 dark:border-gray-800">
        <button @click="openEdit" class="access-meta text-gray-500 hover:text-gray-900 dark:hover:text-gray-100">{{ $t('data.manageConnection') }}</button>
      </footer>

      <!-- Specialized connector editors retain their existing management forms. -->
      <ConnectionDeleteAction v-if="canUpdateDataSource && (isToolShape || isIntegrationManaged)" :connection="connection" @deleted="emit('updated'); isOpen = false" />
      <!-- Test Result -->
      <div v-if="testResult" class="mt-3 text-xs text-center" :class="testResult.success ? 'text-green-600' : 'text-red-600'">
        {{ testResult.message }}
      </div>


    </div>
  </UModal>

  <!-- Edit Connection Modal -->
  <EditConnectionModal v-model="showEditModal" :connection="connection" :initial-values="editFormValues" :loading="loadingDetails" :last-test="editLastTest" @success="handleTested" @tested="handleTested" @deleted="handleEditSuccess" />

  <!-- MCP Edit Modal -->
  <AddMCPModal
    v-model="showMcpEditModal"
    :editConnection="connection"
    @created="handleEditSuccess"
  />

  <!-- Custom API Edit Modal -->
  <AddCustomAPIModal
    v-model="showCustomApiEditModal"
    :editConnection="connection"
    @created="handleEditSuccess"
  />

  <!-- Integration Edit Modal (OneDrive, Google Drive, Outlook, Gmail, …) -->
  <UModal v-model="showIntegrationEditModal" :ui="{ width: 'sm:max-w-lg' }">
    <div class="p-6">
      <div class="flex items-center gap-2 mb-4">
        <DataSourceIcon :type="connection?.type" :connector-key="connection?.connector_key" class="h-5" />
        <h2 class="text-lg font-semibold">{{ $t('data.editConnection') }}</h2>
      </div>
      <IntegrationConnectionForm
        v-if="showIntegrationEditModal"
        :integration-type="connection?.type"
        :integration-title="connection?.name"
        :editConnection="connection"
        @saved="handleIntegrationEditSaved"
        @cancel="showIntegrationEditModal = false"
      />
    </div>
  </UModal>

  <!-- User Credentials Modal (for users without update permission but require auth) -->
  <!-- The modal derives the connection id from a data-source-shaped object
       (.connections[0].id), so wrap the connection to satisfy that contract. -->
  <UserDataSourceCredentialsModal
    v-model="showCredentialsModal"
    :dataSource="connectionAsDataSource"
    @saved="handleCredentialsSaved"
  />
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import EditConnectionModal from '~/components/EditConnectionModal.vue'
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'
import ConnectionIndexingProgress from '~/components/ConnectionIndexingProgress.vue'
import AddMCPModal from '~/components/AddMCPModal.vue'
import AddCustomAPIModal from '~/components/AddCustomAPIModal.vue'
import IntegrationConnectionForm from '~/components/IntegrationConnectionForm.vue'
import { useCan } from '~/composables/usePermissions'
import { getEffectiveStatus, statusDotClass, statusLabelKey, isIndexingActive, type ConnectionIndexing } from '~/composables/useConnectionStatus'
import { useEnterprise } from '~/ee/composables/useEnterprise'

const props = defineProps<{
  modelValue: boolean
  connection: any
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'updated'): void
}>()

const { t } = useI18n()
const toast = useToast()
const signIn = useConnectionSignIn()
// True while awaiting the OAuth authorize redirect; spins the Connect button.
const connecting = ref(false)

const isOpen = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value)
})

const testing = ref(false)
const testResult = ref<{ success: boolean; message: string } | null>(null)
const showEditModal = ref(false)
const showMcpEditModal = ref(false)
const showCustomApiEditModal = ref(false)
const showIntegrationEditModal = ref(false)
const loadingDetails = ref(false)
const connectionDetails = ref<any>(null)
const showCredentialsModal = ref(false)
const indexingState = ref<ConnectionIndexing | null>(null)
let pollGeneration = 0
let pollTimer: ReturnType<typeof setInterval> | null = null
const POLL_INTERVAL_MS = 2000

// Permission and auth checks
const canUpdateDataSource = computed(() => useCan('manage_connection', { type: 'connection', id: props.connection?.id }))
const requiresUserAuth = computed(() => props.connection?.auth_policy === 'user_required')
// Locally-overridable user status: the query-identity PATCH returns a fresh status
// which we apply immediately, so the modal reflects the switch without waiting on
// (or depending on) the parent re-passing the connection prop.
const statusOverride = ref<any>(null)
// Optimistic identity selection — highlights the chosen button the instant it's
// clicked, before the request returns; cleared once the authoritative status lands.
const pendingIdentity = ref<'self' | 'service_account' | null>(null)
const userStatus = computed(() => statusOverride.value || detail.value?.user_status || props.connection?.user_status || null)
const hasUserCredentials = computed(() => !!userStatus.value?.has_user_credentials)
// Owner/admin runs via the connection's system (service principal) creds.
const usesServiceAccount = computed(() => userStatus.value?.effective_auth === 'system')
// Non-admin viewer running on their own per-user (OBO) creds: show a user-scoped
// summary instead of the shared service-principal indexing run + logs.
const isPerUserViewer = computed(() =>
  requiresUserAuth.value && hasUserCredentials.value && !canUpdateDataSource.value
)
// Admin/owner query-identity toggle (delegated/OBO connections).
const canSwitchIdentity = computed(() => !!userStatus.value?.can_switch_identity)
const queryIdentity = computed<'self' | 'service_account'>(() =>
  (pendingIdentity.value
    ?? (userStatus.value?.query_identity === 'service_account' ? 'service_account' : 'self'))
)
const switchingIdentity = ref(false)
async function setIdentity(identity: 'self' | 'service_account') {
  if (!props.connection?.id || switchingIdentity.value) return
  if (queryIdentity.value === identity) return
  pendingIdentity.value = identity // optimistic highlight
  switchingIdentity.value = true
  try {
    const { data, error } = await useMyFetch(`/connections/${props.connection.id}/query-identity`, {
      method: 'PATCH',
      body: { query_identity: identity },
    })
    if (error.value) {
      pendingIdentity.value = null
      toast.add({
        title: t('data.switchIdentityFailed'),
        description: (error.value as any)?.data?.detail || (error.value as any)?.message,
        color: 'red',
      })
    } else {
      // Apply the authoritative status returned by the endpoint, then let the
      // parent refresh table counts / overlays in the background.
      if (data.value) statusOverride.value = data.value
      pendingIdentity.value = null
      emit('updated')
    }
  } catch (e: any) {
    pendingIdentity.value = null
    toast.add({ title: t('data.switchIdentityFailed'), description: e?.message, color: 'red' })
  } finally {
    switchingIdentity.value = false
  }
}
const disconnecting = ref(false)
// The credentials modal expects a data-source-shaped object whose
// `connections[0].id` is the connection to authorize. We only have the
// connection here, so wrap it.
const connectionAsDataSource = computed(() =>
  props.connection ? { ...props.connection, connections: [props.connection] } : null
)

// Types created/edited via the MCP-style integration modal and exempt from
// the SQL-flavored enterprise sections (auto-reindex, rate limit). Distinct
// from data_shape: OneDrive is files-shaped but still integration-managed.
const _INTEGRATION_TYPES = [
  'mcp', 'custom_api', 'onedrive', 'google_drive', 'outlook_mail', 'gmail_mail',
]
const isIntegrationManaged = computed(() => _INTEGRATION_TYPES.includes(props.connection?.type))

// Registry data_shape carried on the connection payload — drives which count
// and noun the modal shows ("Files 12", not "Tables 0" for OneDrive). Older
// payloads without data_shape fall back to the tools/tables binary.
const dataShape = computed(() => props.connection?.data_shape
  || (['mcp', 'custom_api'].includes(props.connection?.type) ? 'tools' : 'tables'))
const isToolShape = computed(() => dataShape.value === 'tools')
const toolCount = computed(() => props.connection?.tool_count || 0)

const countLabel = computed(() => {
  if (dataShape.value === 'tools') return t('data.toolsLabel')
  if (dataShape.value === 'files') return t('data.filesLabel')
  if (dataShape.value === 'objects') return t('data.collectionsLabel')
  return t('data.tablesLabel')
})

// Per-user viewer summary ("{n} files accessible"), shape-aware.
const accessibleSummary = computed(() => {
  if (dataShape.value === 'tools') return t('data.toolsAccessible', { n: toolCount.value })
  if (dataShape.value === 'files') return t('data.filesAccessible', { n: tableCount.value })
  if (dataShape.value === 'objects') return t('data.collectionsAccessible', { n: tableCount.value })
  return t('data.tablesAccessible', { n: tableCount.value })
})

const accessibleAgents = ref<Array<{id: string; name: string; icon?: string | null}>>([])
const loadingAgents = ref(false)
const agentsError = ref(false)
let agentsRequest = 0
const identityOptions = computed(() => [
  { value: 'self' as const, label: t('data.myAccount'), description: t('data.personalAccessDescription') },
  { value: 'service_account' as const, label: t('data.organizationAccount'), description: t('data.organizationAccessDescription') },
])
const canChooseIdentity = computed(() => canSwitchIdentity.value && canUpdateDataSource.value)
const visibleIdentityOptions = computed(() => canChooseIdentity.value ? identityOptions.value
  : identityOptions.value.filter(option => option.value === (personalAccess.value ? 'self' : 'service_account')))
// Personal token status does not establish shared connection health. Render
// shared health only when the payload actually provides it, otherwise show the
// personal result beside the account it describes.
const sharedTestStatus = computed(() => detail.value?.last_connection_status || props.connection?.last_connection_status
  || (!personalAccess.value ? userStatus.value?.connection : null))
const headerStatus = computed(() => {
  if (requiresUserAuth.value && !sharedTestStatus.value) return null
  return getEffectiveStatus({
    last_connection_status: sharedTestStatus.value,
    indexing: props.connection?.indexing?.scope === 'user' ? null : props.connection?.indexing,
  })
})
watch(() => [props.modelValue, props.connection?.id], async () => {
  const request = ++agentsRequest
  accessibleAgents.value = []
  agentsError.value = false
  if (!props.modelValue || !props.connection?.id) return
  loadingAgents.value = true
  try {
    const { data, error } = await useMyFetch(`/connections/${props.connection.id}/accessible-agents`, { method: 'GET' })
    if (request !== agentsRequest) return
    agentsError.value = !!error.value
    if (Array.isArray(data.value)) accessibleAgents.value = data.value
  } catch { if (request === agentsRequest) agentsError.value = true }
  finally { if (request === agentsRequest) loadingAgents.value = false }
}, { immediate: true })
const personalAccess = computed(() => requiresUserAuth.value && (canSwitchIdentity.value ? queryIdentity.value === 'self' : !usesServiceAccount.value))
const needsSignIn = computed(() => personalAccess.value && !hasUserCredentials.value)
const accessStatus = computed(() => getEffectiveStatus({
  ...props.connection, ...detail.value,
  user_status: personalAccess.value ? userStatus.value : { connection: detail.value?.last_connection_status || detail.value?.user_status?.connection || props.connection?.last_connection_status || userStatus.value?.connection },
  indexing: personalAccess.value ? indexingState.value : detail.value?.indexing || props.connection?.indexing,
}))
const availableCount = computed(() => personalAccess.value
  ? (indexingState.value?.stats?.table_count ?? myTableCountOverride.value ?? null)
  : isToolShape.value ? toolCount.value : tableCount.value)
const personalWarnings = computed(() => personalAccess.value && indexingState.value?.status === 'completed' ? (indexingState.value.events || []).filter(e => ['warning', 'warn', 'error'].includes(e.level)).slice(-3) : [])
async function handleTested() { await fetchDetail(); emit('updated') }

// Prefer a freshly-reloaded per-user count (set by reloadMySchema) over the
// value carried on the connection prop, so the count updates without waiting
// for the parent to refetch the connections list.
const myTableCountOverride = ref<number | null>(null)

// Call sites pass connection objects of varying shape (the org list, an agent's
// connection chip, a freshly-created record), so the counts are read from the
// detail endpoint when the modal opens rather than trusted from the prop.
// One GET serves counts, the auto-reindex schedule and the rate-limit config
// (they all live on the same payload — this used to be three identical calls).
const detail = ref<any>(null)
async function fetchDetail() {
  const id = props.connection?.id
  if (!id) return
  try {
    const { data, error } = await useMyFetch(`/connections/${id}`, { method: 'GET' })
    if (!error.value && data.value) {
      detail.value = data.value
    }
  } catch { /* fall back to whatever the prop carried */ }
}
watch(() => props.modelValue, (open) => {
  if (!open) { detail.value = null; return }
  fetchDetail()
})

const tableCount = computed(
  () => myTableCountOverride.value ?? detail.value?.table_count ?? (props.connection?.table_count || 0))
const agentCount = computed(() => detail.value?.agent_count ?? (props.connection?.agent_count || 0))
const customQueriesCount = computed(
  () => detail.value?.custom_queries_count ?? props.connection?.custom_queries_count ?? 0)
const customQueriesSupported = computed(
  () => !!(detail.value?.custom_queries_supported ?? props.connection?.custom_queries_supported))
const agentNames = computed(() => detail.value?.agent_names ?? (props.connection?.agent_names || []))

function checkedAgo(ts: string | null | undefined) {
  if (!ts) return null
  const seconds = Math.floor((Date.now() - new Date(ts).getTime()) / 1000)
  if (!Number.isFinite(seconds)) return null
  if (seconds < 60) return t('data.justNow')
  if (seconds < 3600) return t('data.minutesAgo', { n: Math.floor(seconds / 60) })
  if (seconds < 86400) return t('data.hoursAgo', { n: Math.floor(seconds / 3600) })
  return t('data.daysAgo', { n: Math.floor(seconds / 86400) })
}
const headerCheckedDisplay = computed(() => checkedAgo(detail.value?.last_connection_checked_at || props.connection?.last_connection_checked_at
  || (!personalAccess.value ? detail.value?.last_checked_at || props.connection?.last_checked_at || userStatus.value?.last_checked_at : null)))
const personalCheckedDisplay = computed(() => checkedAgo(userStatus.value?.last_checked_at || userStatus.value?.last_used_at))

const lastIndexedDisplay = computed(() => {
  const ts = indexingState.value?.finished_at
  if (!ts) return ''
  const seconds = Math.floor((Date.now() - new Date(ts).getTime()) / 1000)
  if (seconds < 60) return t('data.justNow')
  if (seconds < 3600) return t('data.minutesAgo', { n: Math.floor(seconds / 60) })
  if (seconds < 86400) return t('data.hoursAgo', { n: Math.floor(seconds / 3600) })
  return t('data.daysAgo', { n: Math.floor(seconds / 86400) })
})

// When THIS user last pulled their accessible tables. Prefer a fresh local
// timestamp set right after a per-user reload; otherwise the last successful
// use of their creds from the connection payload.
const myRefreshedAt = ref<string | null>(null)
const myLastRefreshedDisplay = computed(() => {
  const ts = myRefreshedAt.value || props.connection?.user_status?.last_used_at
  if (!ts) return ''
  const seconds = Math.floor((Date.now() - new Date(ts).getTime()) / 1000)
  if (seconds < 60) return t('data.justNow')
  if (seconds < 3600) return t('data.minutesAgo', { n: Math.floor(seconds / 60) })
  if (seconds < 86400) return t('data.hoursAgo', { n: Math.floor(seconds / 3600) })
  return t('data.daysAgo', { n: Math.floor(seconds / 86400) })
})

async function fetchIndexing() {
  const generation = pollGeneration
  const id = props.connection?.id
  // Tool providers (MCP / Custom API) have no schema-indexing runs — the
  // endpoint would just 404 on every open.
  if (!props.connection?.id || !requiresUserAuth.value) return
  try {
    const { data } = await useMyFetch(`/connections/${props.connection.id}/indexing?scope=user`, { method: 'GET' })
    if (generation !== pollGeneration || props.connection?.id !== id || !isOpen.value) return
    if ((data as any).value) {
      indexingState.value = (data as any).value as ConnectionIndexing
      if (indexingState.value?.status === 'completed') myRefreshedAt.value = indexingState.value.finished_at || null
    }
  } catch {
    // 404 = no indexing run ever; transient errors handled silently.
  }
}

function startPollingIfActive() {
  stopPolling()
  if (!isIndexingActive(indexingState.value)) return
  pollTimer = setInterval(() => {
    if (!isOpen.value || !isIndexingActive(indexingState.value)) {
      stopPolling()
      return
    }
    fetchIndexing()
  }, POLL_INTERVAL_MS)
}

function stopPolling() {
  pollGeneration++
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

const editFormValues = computed(() => {
  if (!connectionDetails.value) return null
  return {
    management_auth: connectionDetails.value.management_auth,
    last_connection_status: connectionDetails.value.last_connection_status,
    last_connection_checked_at: connectionDetails.value.last_connection_checked_at,
    name: connectionDetails.value.name,
    config: connectionDetails.value.config || {},
    auth_policy: connectionDetails.value.auth_policy,
    has_credentials: connectionDetails.value.has_credentials,
    credentials: connectionDetails.value.credentials_meta || {}
  }
})

async function testConnection() {
  if (!props.connection?.id || testing.value) return
  testing.value = true
  testResult.value = null
  try {
    const { data, error } = await useMyFetch(`/connections/${props.connection.id}/test`, { method: 'POST' })
    if (error.value) {
      testResult.value = { success: false, message: error.value.message || t('data.testFailed') }
    } else {
      const result = data.value as any
      testResult.value = {
        success: result.success,
        message: result.success ? t('data.connectionSuccessful') : (result.message || t('data.connectionFailed'))
      }
    }
    emit('updated')
  } catch (e: any) {
    testResult.value = { success: false, message: e.message || t('data.testFailed') }
  } finally {
    testing.value = false
  }
}

const editLastTest = ref<{ success: boolean; message: string } | null>(null)
async function openEdit() {
  editLastTest.value = testResult.value
  isOpen.value = false
  await nextTick()

  // Each integration-managed type gets its own edit form — an MCP server,
  // a Custom API, and an OAuth integration (OneDrive, Gmail, …) store
  // completely different config shapes, so routing them all to one form
  // would rewrite the connection's config with the wrong fields on save.
  if (props.connection?.type === 'mcp') {
    showMcpEditModal.value = true
    return
  }
  if (props.connection?.type === 'custom_api') {
    showCustomApiEditModal.value = true
    return
  }
  if (isIntegrationManaged.value) {
    showIntegrationEditModal.value = true
    return
  }

  loadingDetails.value = true
  showEditModal.value = true

  try {
    const { data } = await useMyFetch(`/connections/${props.connection.id}`, { method: 'GET' })
    if (data.value) {
      connectionDetails.value = data.value
    }
  } finally {
    loadingDetails.value = false
  }
}

function handleEditSuccess() {
  showEditModal.value = false
  connectionDetails.value = null
  emit('updated')
}

function handleIntegrationEditSaved() {
  showIntegrationEditModal.value = false
  emit('updated')
}

async function openCredentialsModal() {
  // OAuth-only (Entra/OBO) connections have nothing to type or pick — redirect
  // straight to the provider instead of opening an empty credentials modal,
  // collapsing the old Connect → Sign in two-click flow into one. The button
  // keeps spinning through the slow authorize round-trip until the browser
  // navigates away. Anything else (multiple auth modes, no oauth) falls back
  // to the modal as before.
  connecting.value = true
  const result = await signIn.triggerUserSignIn(props.connection)
  if (result.redirecting) return // keep spinning; the page is navigating to the provider
  connecting.value = false
  if (result.error) {
    toast.add({ title: t('data.oauthStartFailed'), description: result.error, color: 'red' })
  }
  isOpen.value = false
  showCredentialsModal.value = true
}

const reloadingMySchema = ref(false)
const personalRefreshError = ref('')
async function reloadMySchema() {
  if (!props.connection?.id || reloadingMySchema.value || isIndexingActive(indexingState.value)) return
  reloadingMySchema.value = true
  personalRefreshError.value = ''
  const id = props.connection.id
  try {
    const { data, error } = await useMyFetch(`/connections/${id}/my-schema/refresh?background=true`, { method: 'POST' })
    if (!isOpen.value || props.connection?.id !== id) return
    if (error.value) { personalRefreshError.value = (error.value as any)?.data?.detail || t('data.reindexFailed'); return }
    indexingState.value = (data.value as any)?.indexing || null
    startPollingIfActive()
  } catch {
    personalRefreshError.value = t('data.requestFailed')
  } finally { reloadingMySchema.value = false }
}

async function disconnect() {
  // Per-user creds are CONNECTION-level — clear them via the connection endpoint.
  if (!props.connection?.id || disconnecting.value) return
  disconnecting.value = true
  try {
    await useMyFetch(`/connections/${props.connection.id}/my-credentials`, { method: 'DELETE' })
    emit('updated')
    isOpen.value = false
  } finally {
    disconnecting.value = false
  }
}

function handleCredentialsSaved() {
  emit('updated')
}

// Reset state when modal closes
watch(isOpen, (val) => {
  if (!val) {
    testResult.value = null
    connecting.value = false
    stopPolling()
    return
  }
  // Modal opened — seed indexing state from props, fetch fresh, then poll
  // if active.
  personalRefreshError.value = ''
  myTableCountOverride.value = null
  myRefreshedAt.value = null
  statusOverride.value = null
  pendingIdentity.value = null
  indexingState.value = props.connection?.indexing?.scope === 'user' ? props.connection.indexing : null
  fetchIndexing().then(() => startPollingIfActive())
})

// If the parent swaps the connection prop while the modal is open, refresh.
watch(() => props.connection?.id, () => {
  if (!isOpen.value) return
  statusOverride.value = null
  pendingIdentity.value = null
  indexingState.value = props.connection?.indexing?.scope === 'user' ? props.connection.indexing : null
  fetchIndexing().then(() => startPollingIfActive())
  fetchDetail()
})

onBeforeUnmount(() => stopPolling())
</script>

<style scoped>
.connection-access { font-size: 13px; line-height: 20px; font-weight: 400; }
.connection-access h2 { font-size: 16px; line-height: 24px; }
.access-meta { font-size: 12px; line-height: 18px; }
.access-button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 36px; padding: 6px 12px; border-radius: 6px; font-size: 12px; line-height: 18px; font-weight: 500; }
.access-button:disabled { opacity: 0.5; }
</style>
