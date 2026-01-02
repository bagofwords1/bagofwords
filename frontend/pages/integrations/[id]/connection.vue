<template>
    <div class="py-6">
        <div class="bg-white border border-gray-200 rounded-lg p-6">
            <!-- Loading state -->
            <div v-if="!integration" class="text-sm text-gray-500">Loading...</div>

            <!-- Main content - View Mode -->
            <div v-else>
                <div class="flex items-center justify-between">
                    <div class="flex items-center gap-3">
                        <DataSourceIcon :type="connectionType" class="h-8" />
                        <div>
                            <div class="font-semibold text-gray-900">{{ connectionName }}</div>
                            <div class="text-xs text-gray-500">{{ connectionType }}</div>
                        </div>
                    </div>
                    <div class="flex items-center gap-2">
                        <!-- Connection status badge -->
                        <span :class="['px-2 py-0.5 rounded text-xs border flex items-center gap-1', connectionStatusClass]">
                            {{ connectionStatusLabel }}
                        </span>
                        <!-- Last checked time -->
                        <span v-if="lastCheckedDisplay" class="text-[10px] text-gray-400">
                            {{ lastCheckedDisplay }}
                        </span>
                        <!-- Test/Refresh button -->
                        <button 
                            @click="testConnection" 
                            :disabled="isTesting"
                            class="p-1.5 rounded hover:bg-gray-100 disabled:opacity-50"
                            title="Test connection"
                        >
                            <Spinner v-if="isTesting" class="w-4 h-4" />
                            <UIcon v-else name="heroicons-arrow-path" class="w-4 h-4 text-gray-500" />
                        </button>
                        <!-- Edit button - only for users with manage_connections permission -->
                        <UButton 
                            v-if="canManageConnections"
                            color="gray" 
                            variant="ghost" 
                            size="xs"
                            @click="showEditModal = true"
                        >
                            <UIcon name="heroicons-pencil" class="w-5 h-5" />
                        </UButton>
                    </div>
                </div>

                <!-- Test result (inline) -->
                <div v-if="testConnectionStatus !== null" class="mt-2 ml-11 text-xs">
                    <span :class="testConnectionStatus?.success ? 'text-green-600' : 'text-red-600'">
                        {{ testConnectionStatus?.success ? 'Connection successful' : (testConnectionStatus?.message || 'Connection failed') }}
                    </span>
                </div>

                <!-- User Connection (only for user_required auth, non-admin) -->
                <div class="mt-4 ml-11" v-if="connectionAuthPolicy === 'user_required' && !isAdmin">
                    <div class="text-sm text-gray-800 flex items-center space-x-3">
                        <template v-if="connectionUserStatus?.has_user_credentials">
                            <span class="inline-flex items-center text-green-700 text-xs">
                                <UIcon name="heroicons-check-circle" class="w-3 h-3 mr-1" />
                                Connected as {{ connectedUserDisplay }}
                            </span>
                            <UButton size="xs" color="gray" variant="ghost" :loading="isTestingUser" @click="testUserConnection">
                                <UIcon name="heroicons-play" class="w-4 h-4" />
                            </UButton>
                            <UButton size="xs" color="red" variant="ghost" @click="disconnectUserCredentials">Disconnect</UButton>
                        </template>
                        <template v-else>
                            <span class="inline-flex items-center text-gray-500 text-xs">
                                <UIcon name="heroicons-exclamation-circle" class="w-3 h-3 mr-1" />
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
    <UModal v-model="showEditModal" :ui="{ width: 'sm:max-w-xl' }">
        <UCard>
            <template #header>
                <div class="flex items-center justify-between">
                    <div class="flex items-center gap-2">
                        <DataSourceIcon :type="connectionType" class="h-5 w-5" />
                        <h3 class="text-lg font-semibold">Edit Connection</h3>
                    </div>
                    <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark" @click="showEditModal = false" />
                </div>
            </template>

            <ConnectForm
                v-if="showEditModal"
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
definePageMeta({ auth: true, layout: 'integrations' })
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

// Inject integration data from layout (avoid duplicate API calls)
const injectedIntegration = inject<Ref<any>>('integration', ref(null))
const injectedFetchIntegration = inject<() => Promise<void>>('fetchIntegration', async () => {})

// Use injected data
const integration = injectedIntegration

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

// Connection data accessors
const connectionId = computed(() => integration.value?.connection?.id || null)
const connectionType = computed(() => integration.value?.connection?.type || integration.value?.type)
const connectionName = computed(() => integration.value?.connection?.name || integration.value?.name || 'Connection')
const connectionConfig = computed(() => integration.value?.connection?.config || integration.value?.config || {})
const connectionAuthPolicy = computed(() => integration.value?.connection?.auth_policy || integration.value?.auth_policy || 'system_only')
const connectionUserStatus = computed(() => integration.value?.connection?.user_status || integration.value?.user_status)
const hasCredentials = computed(() => integration.value?.connection?.has_credentials ?? true)

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
    if (c === 'success') return 'bg-green-50 text-green-700 border-green-200'
    if (c === 'not_connected' || c === 'offline') return 'bg-red-50 text-red-700 border-red-200'
    return 'bg-gray-50 text-gray-700 border-gray-200'
})

// Last checked display
const lastCheckedAt = computed(() => connectionUserStatus.value?.last_checked_at)
const lastCheckedDisplay = computed(() => {
    if (!lastCheckedAt.value) return null
    return `Checked ${timeAgo(lastCheckedAt.value)}`
})

function timeAgo(date: string) {
    const seconds = Math.floor((Date.now() - new Date(date).getTime()) / 1000)
    if (seconds < 60) return 'just now'
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
    return `${Math.floor(seconds / 86400)}d ago`
}

// Form initial values for editing
const editFormInitialValues = computed(() => ({
  name: connectionName.value,
  config: connectionConfig.value,
  auth_policy: connectionAuthPolicy.value,
  has_credentials: hasCredentials.value,
  credentials: {}
}))

async function testConnection() {
  if (!dsId.value || isTesting.value) return
  isTesting.value = true
  testConnectionStatus.value = null
  try {
    const response = await useMyFetch(`/data_sources/${dsId.value}/test_connection`, { method: 'GET' })
    testConnectionStatus.value = (response.data as any)?.value || null
    // Refresh integration data from layout
    await injectedFetchIntegration()
  } finally {
    isTesting.value = false
  }
}

function handleEditSuccess() {
  showEditModal.value = false
  injectedFetchIntegration()
}

function openAddCredentials() {
  showCredsModal.value = true
}

async function disconnectUserCredentials() {
  if (!dsId.value) return
  try {
    await useMyFetch(`/data_sources/${dsId.value}/my-credentials`, { method: 'DELETE' })
    await injectedFetchIntegration()
  } catch (e) {
    // no-op
  }
}

async function onCredsSaved() {
  showCredsModal.value = false
  await injectedFetchIntegration()
}

async function testUserConnection() {
  if (!dsId.value || isTestingUser.value) return
  isTestingUser.value = true
  try {
    const response = await useMyFetch(`/data_sources/${dsId.value}/test_connection`, { method: 'GET' })
    testUserStatus.value = (response.data as any)?.value || null
    // Refresh integration data from layout
    await injectedFetchIntegration()
  } finally {
    isTestingUser.value = false
  }
}
</script>


