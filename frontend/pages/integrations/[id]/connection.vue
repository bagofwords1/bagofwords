<template>
    <div class="py-6">
        <div class="bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg p-6">
            <!-- Loading / error / empty states -->
            <div v-if="isLoading" class="text-sm text-gray-500 dark:text-gray-400" data-testid="connection-loading">{{ $t('common.loading') }}</div>
            <div v-else-if="fetchError" class="text-sm text-gray-600 dark:text-gray-300" role="alert" data-testid="connection-error">
                {{ fetchErrorMessage }}
            </div>
            <div v-else-if="!connection" class="text-sm text-gray-500 dark:text-gray-400" data-testid="connection-empty">
                {{ $t('integrationConnection.noConnection') }}
            </div>

            <!-- Main content - View Mode -->
            <div v-else>
                <div class="flex items-center justify-between">
                    <div class="flex items-center gap-3">
                        <DataSourceIcon :type="connectionType" class="h-8" />
                        <div>
                            <div class="font-semibold text-gray-900 dark:text-white">{{ connectionName }}</div>
                            <div class="text-xs text-gray-500 dark:text-gray-400">{{ connectionType }}</div>
                        </div>
                    </div>
                    <div class="flex items-center gap-2">
                        <!-- Connection status badge -->
                        <span :class="['px-2 py-0.5 rounded text-xs border flex items-center gap-1', connectionStatusClass]">
                            {{ connectionStatusLabel }}
                        </span>
                        <!-- Last checked time -->
                        <span v-if="lastCheckedDisplay" class="text-[10px] text-gray-400 dark:text-gray-600">
                            {{ lastCheckedDisplay }}
                        </span>
                        <!-- Test/Refresh button -->
                        <button 
                            @click="testConnection" 
                            :disabled="isTesting"
                            class="p-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50"
                            title="Test connection"
                        >
                            <Spinner v-if="isTesting" class="w-4 h-4" />
                            <UIcon v-else name="heroicons-arrow-path" class="w-4 h-4 text-gray-500 dark:text-gray-400" />
                        </button>
                        <!-- Edit button - only for users with manage_connections permission -->
                        <UButton 
                            v-if="canManageConnections"
                            color="gray" 
                            variant="ghost" 
                            size="xs"
                            data-testid="connection-edit"
                            @click="openEditModal"
                        >
                            <UIcon name="heroicons-pencil" class="w-5 h-5" />
                        </UButton>
                    </div>
                </div>

                <!-- Test result (inline) -->
                <div v-if="testConnectionStatus !== null" class="mt-2 ms-11 text-xs">
                    <span :class="testConnectionStatus?.success ? 'text-green-600' : 'text-red-600'">
                        {{ testConnectionStatus?.success ? 'Connection successful' : (testConnectionStatus?.message || 'Connection failed') }}
                    </span>
                </div>

                <!-- User Connection (only for user_required auth, non-admin) -->
                <div class="mt-4 ms-11" v-if="connectionAuthPolicy === 'user_required' && !isAdmin">
                    <div class="text-sm text-gray-800 dark:text-gray-200 flex items-center space-x-3">
                        <template v-if="connectionUserStatus?.has_user_credentials">
                            <span class="inline-flex items-center text-green-700 text-xs">
                                <UIcon name="heroicons-check-circle" class="w-3 h-3 me-1" />
                                Connected as {{ connectedUserDisplay }}
                            </span>
                            <UButton size="xs" color="gray" variant="ghost" :loading="isTestingUser" @click="testUserConnection">
                                <UIcon name="heroicons-play" class="w-4 h-4" />
                            </UButton>
                            <UButton size="xs" color="red" variant="ghost" @click="disconnectUserCredentials">Disconnect</UButton>
                        </template>
                        <template v-else>
                            <span class="inline-flex items-center text-gray-500 dark:text-gray-400 text-xs">
                                <UIcon name="heroicons-exclamation-circle" class="w-3 h-3 me-1" />
                                User credentials required
                            </span>
                            <UButton size="xs" color="blue" variant="soft" @click="openAddCredentials">Connect</UButton>
                        </template>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Edit Connection Modal -->
    <UModal v-if="canManageConnections" v-model="showEditModal" :ui="{ width: 'sm:max-w-xl' }">
        <UCard>
            <template #header>
                <div class="flex items-center justify-between">
                    <div class="flex items-center gap-2">
                        <DataSourceIcon :type="connectionType" class="h-5" />
                        <h3 class="text-lg font-semibold">Edit Connection</h3>
                    </div>
                    <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark" @click="showEditModal = false" />
                </div>
            </template>

            <div v-if="loadingConnectionDetails" class="text-sm text-gray-500 dark:text-gray-400">{{ $t('common.loading') }}</div>
            <div v-else-if="connectionDetailsError" class="text-sm text-red-600" role="alert">{{ $t('integrationConnection.loadFailed') }}</div>
            <ConnectForm
                v-else-if="showEditModal && connectionDetails"
                mode="edit"
                :connection-id="connectionId"
                :initial-type="connectionType"
                :initial-values="editFormInitialValues"
                :show-test-button="true"
                :show-llm-toggle="false"
                :allow-name-edit="true"
                :force-show-system-credentials="true"
                :show-require-user-auth-toggle="true"
                :hide-header="true"
                @success="handleEditSuccess"
            />
        </UCard>
    </UModal>

    <!-- Modal for managing user credentials -->
    <UserDataSourceCredentialsModal v-model="showCredsModal" :data-source="integration" @saved="onCredsSaved" />
</template>

<script setup lang="ts">
// No custom layout: the `integrations` layout this page used to inject its
// data from was renamed away in 6f352fbdb, so the page loads its own data.
definePageMeta({ auth: true })
import ConnectForm from '~/components/datasources/ConnectForm.vue'
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'
import Spinner from '~/components/Spinner.vue'
import { useCan } from '~/composables/usePermissions'
import { useOrganization } from '~/composables/useOrganization'
import type { Ref } from 'vue'

const route = useRoute()
const dsId = computed(() => String(route.params.id || ''))
const canManageConnections = computed(() => useCan('manage_connections'))
const { data: currentUser } = useAuth()
const { organization } = useOrganization()

const { t } = useI18n()

// The data source (agent) this connection belongs to. GET /data_sources/{id}
// omits connection config for view-only members, so `config` is only ever
// read from GET /connections/{id} (manage-only) when the edit modal opens.
const integration = ref<any>(null)
const isLoading = ref(true)
const fetchError = ref<number | null>(null)

async function fetchIntegration() {
  if (!dsId.value) return
  try {
    const { data, error } = await useMyFetch(`/data_sources/${dsId.value}`, { method: 'GET' })
    if (error.value) {
      const err: any = error.value
      fetchError.value = err?.statusCode || err?.status || err?.response?.status || 500
      return
    }
    integration.value = data.value || null
    fetchError.value = null
  } catch (e: any) {
    fetchError.value = e?.statusCode || e?.status || 500
  } finally {
    isLoading.value = false
  }
}

watch(dsId, () => {
  isLoading.value = true
  integration.value = null
  fetchIntegration()
}, { immediate: true })

const fetchErrorMessage = computed(() => {
  if (fetchError.value === 404) return t('integrationConnection.notFound')
  if (fetchError.value === 403) return t('integrationConnection.forbidden')
  return t('integrationConnection.loadFailed')
})

const isTesting = ref(false)
const testConnectionStatus = ref<any>(null)
const showEditModal = ref(false)
const showCredsModal = ref(false)
const isTestingUser = ref(false)
const testUserStatus = ref<any>(null)

const connectedUserDisplay = computed(() => {
  const u = (currentUser.value as any) || {}
  return u.name || u.email || 'You'
})

const isAdmin = computed(() => {
  const orgs = (((currentUser.value as any) || {}).organizations || [])
  const org = orgs.find((o: any) => o.id === organization.value?.id)
  return org?.role === 'admin'
})

// Connection data accessors. The API returns `connections: []`; the first
// one is the primary connection (same convention as UserDataSourceCredentialsModal).
const connection = computed(() => integration.value?.connections?.[0] || null)
const connectionId = computed(() => connection.value?.id || null)
const connectionType = computed(() => connection.value?.type || integration.value?.type)
const connectionName = computed(() => connection.value?.name || integration.value?.name || 'Connection')
const connectionAuthPolicy = computed(() => connection.value?.auth_policy || integration.value?.auth_policy || 'system_only')
const connectionUserStatus = computed(() => connection.value?.user_status || integration.value?.user_status)

// Manager-only connection details (config + has_credentials), loaded lazily.
const connectionDetails = ref<any>(null)
const loadingConnectionDetails = ref(false)
const connectionDetailsError = ref(false)

async function openEditModal() {
  if (!canManageConnections.value || !connectionId.value) return
  showEditModal.value = true
  loadingConnectionDetails.value = true
  connectionDetailsError.value = false
  connectionDetails.value = null
  try {
    const { data, error } = await useMyFetch(`/connections/${connectionId.value}`, { method: 'GET' })
    if (error.value) connectionDetailsError.value = true
    else connectionDetails.value = data.value || null
  } catch {
    connectionDetailsError.value = true
  } finally {
    loadingConnectionDetails.value = false
  }
}

// Connection status display
const connectionStatus = computed(() => String(connectionUserStatus.value?.connection || '').toLowerCase())
const connectionStatusLabel = computed(() => {
    const c = connectionStatus.value
    if (c === 'success') return 'Connected'
    if (c === 'not_connected') return 'Not connected'
    if (c === 'offline') return 'Offline'
    if (c === 'unknown' || !c) return 'Unknown'
    return 'Unknown'
})
const connectionStatusClass = computed(() => {
    const c = connectionStatus.value
    if (c === 'success') return 'bg-green-50 dark:bg-green-950 text-green-700 border-green-200'
    if (c === 'not_connected' || c === 'offline') return 'bg-red-50 dark:bg-red-950 text-red-700 border-red-200'
    return 'bg-gray-50 dark:bg-gray-900 text-gray-700 dark:text-gray-300 border-gray-200 dark:border-gray-700'
})

// Last checked display
const lastCheckedAt = computed(() => connectionUserStatus.value?.last_checked_at)
const lastCheckedDisplay = computed(() => {
    if (!lastCheckedAt.value) return null
    return `Checked ${timeAgo(lastCheckedAt.value)}`
})

const { relativeTime: timeAgo } = useRelativeTime()

// Form initial values for editing
const editFormInitialValues = computed(() => ({
  name: connectionName.value,
  config: connectionDetails.value?.config || {},
  auth_policy: connectionDetails.value?.auth_policy || connectionAuthPolicy.value,
  has_credentials: connectionDetails.value?.has_credentials ?? true,
  credentials: {}
}))

async function testConnection() {
  if (!dsId.value || isTesting.value) return
  isTesting.value = true
  testConnectionStatus.value = null
  try {
    const response = await useMyFetch(`/data_sources/${dsId.value}/test_connection`, { method: 'GET' })
    testConnectionStatus.value = (response.data as any)?.value || null
    await fetchIntegration()
  } finally {
    isTesting.value = false
  }
}

function handleEditSuccess() {
  showEditModal.value = false
  fetchIntegration()
}

function openAddCredentials() {
  showCredsModal.value = true
}

async function disconnectUserCredentials() {
  if (!dsId.value) return
  try {
    await useMyFetch(`/data_sources/${dsId.value}/my-credentials`, { method: 'DELETE' })
    await fetchIntegration()
  } catch (e) {
    // no-op
  }
}

async function onCredsSaved() {
  showCredsModal.value = false
  await fetchIntegration()
}

async function testUserConnection() {
  if (!dsId.value || isTestingUser.value) return
  isTestingUser.value = true
  try {
    const response = await useMyFetch(`/data_sources/${dsId.value}/test_connection`, { method: 'GET' })
    testUserStatus.value = (response.data as any)?.value || null
    await fetchIntegration()
  } finally {
    isTestingUser.value = false
  }
}
</script>


