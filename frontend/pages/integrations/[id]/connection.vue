<template>
    <div class="py-6">
        <div class="bg-white border border-gray-200 rounded-lg p-6">
            <!-- Loading state -->
            <div v-if="!integration" class="text-sm text-gray-500">Loading...</div>

            <!-- Main content -->
            <div v-else>
                <!-- View Mode -->
                <div v-if="!isEditing">
                    <div class="flex items-center justify-between mb-6">
                        <div class="flex items-center gap-3">
                            <DataSourceIcon :type="connectionType" class="h-6 w-6" />
                            <div>
                                <h2 class="text-lg font-semibold text-gray-900">{{ connectionName }}</h2>
                                <div class="text-xs text-gray-500">{{ connectionType }}</div>
                            </div>
                        </div>
                        <div class="flex items-center gap-2">
                            <UButton v-if="canUpdate" size="sm" color="gray" variant="soft" @click="isEditing = true">
                                <UIcon name="heroicons-pencil" class="w-4 h-4 mr-1" />
                                Edit Connection
                            </UButton>
                            <UButton size="sm" color="gray" variant="soft" :loading="isTesting" @click="testConnection">
                                <UIcon name="heroicons-play" class="w-4 h-4 mr-1" />
                                Test
                            </UButton>
                        </div>
                    </div>

                    <!-- Test result -->
                    <div v-if="testConnectionStatus !== null" class="mb-6 p-3 rounded-lg" :class="testConnectionStatus?.success ? 'bg-green-50' : 'bg-red-50'">
                        <span :class="testConnectionStatus?.success ? 'text-green-700' : 'text-red-700'" class="text-sm flex items-center">
                            <UIcon :name="testConnectionStatus?.success ? 'heroicons-check-circle' : 'heroicons-x-circle'" class="w-4 h-4 mr-2" />
                            {{ testConnectionStatus?.success ? 'Connection successful' : (testConnectionStatus?.message || 'Connection failed') }}
                        </span>
                    </div>

                    <!-- Configuration display -->
                    <div class="space-y-4">
                        <h3 class="text-sm font-medium text-gray-700">Configuration</h3>
                        <div v-if="configFields.length === 0" class="text-xs text-gray-500">No configuration fields.</div>
                        <div v-else class="grid grid-cols-2 gap-4">
                            <div v-for="field in configFields" :key="field.field_name" class="space-y-1">
                                <label class="block text-xs text-gray-500">{{ field.title || field.field_name }}</label>
                                <div class="text-sm text-gray-800">
                                    <span v-if="field.type === 'boolean'">{{ configFormData[field.field_name] ? 'Yes' : 'No' }}</span>
                                    <span v-else-if="isPasswordField(field.field_name)">••••••••</span>
                                    <span v-else>{{ displayValue(configFormData[field.field_name]) }}</span>
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- Credentials status -->
                    <div class="mt-6 pt-6 border-t border-gray-100">
                        <h3 class="text-sm font-medium text-gray-700 mb-3">Credentials</h3>
                        <div class="flex items-center gap-2 text-sm">
                            <span v-if="hasCredentials" class="text-green-600 flex items-center">
                                <UIcon name="heroicons-check-circle" class="w-4 h-4 mr-1" />
                                System credentials configured
                            </span>
                            <span v-else class="text-amber-600 flex items-center">
                                <UIcon name="heroicons-exclamation-circle" class="w-4 h-4 mr-1" />
                                No credentials configured
                            </span>
                        </div>
                        <div class="mt-2 text-xs text-gray-500">
                            Auth policy: <span class="font-medium">{{ connectionAuthPolicy === 'user_required' ? 'User credentials required' : 'System credentials only' }}</span>
                        </div>
                    </div>

                    <!-- User Connection (only for user_required auth) -->
                    <div class="mt-6 pt-6 border-t border-gray-100" v-if="connectionAuthPolicy === 'user_required' && !isAdmin">
                        <h3 class="text-sm font-medium text-gray-700 mb-3">Your Connection</h3>
                        <div class="text-sm text-gray-800 flex items-center space-x-3">
                            <template v-if="connectionUserStatus?.has_user_credentials">
                                <span class="inline-flex items-center text-green-700">
                                    <UIcon name="heroicons-check-circle" class="w-4 h-4 mr-1" />
                                    Connected as {{ connectedUserDisplay }}
                                </span>
                                <UButton size="xs" color="gray" :loading="isTestingUser" @click="testUserConnection">Test</UButton>
                                <UButton size="xs" color="red" variant="soft" @click="disconnectUserCredentials">Disconnect</UButton>
                            </template>
                            <template v-else>
                                <span class="inline-flex items-center text-gray-600">
                                    <UIcon name="heroicons-exclamation-circle" class="w-4 h-4 mr-1" />
                                    Not connected
                                </span>
                                <UButton size="xs" color="blue" variant="solid" @click="openAddCredentials">Add credentials</UButton>
                            </template>
                        </div>
                        <div v-if="testUserStatus !== null" class="mt-2 text-sm">
                            <span v-if="testUserStatus?.success" class="inline-flex items-center text-green-700">
                                <UIcon name="heroicons-check-circle" class="w-4 h-4 mr-1" /> Connected
                            </span>
                            <span v-else class="inline-flex items-center text-red-700">
                                <UIcon name="heroicons-x-circle" class="w-4 h-4 mr-1" /> {{ testUserStatus?.message || 'Failed' }}
                            </span>
                        </div>
                    </div>
                </div>

                <!-- Edit Mode -->
                <div v-else>
                    <div class="flex items-center justify-between mb-4">
                        <h2 class="text-lg font-semibold text-gray-900">Edit Connection</h2>
                        <UButton size="sm" color="gray" variant="ghost" @click="isEditing = false">
                            <UIcon name="heroicons-x-mark" class="w-4 h-4" />
                        </UButton>
                    </div>
                    <ConnectForm
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
                </div>
            </div>
        </div>
    </div>

    <!-- Modal for managing user credentials -->
    <UserDataSourceCredentialsModal v-model="showCredsModal" :data-source="integration" @saved="onCredsSaved" />
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'integrations' })
import ConnectForm from '~/components/datasources/ConnectForm.vue'
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'
import { useCan } from '~/composables/usePermissions'
import { useOrganization } from '~/composables/useOrganization'

const route = useRoute()
const dsId = computed(() => String(route.params.id || ''))
const canUpdate = computed(() => useCan('update_data_source'))
const { data: currentUser } = useAuth()
const { organization } = useOrganization()

const integration = ref<any>(null)
const configFields = ref<any[]>([])
const configFormData = reactive<any>({})

const isTesting = ref(false)
const testConnectionStatus = ref<any>(null)
const isEditing = ref(false)
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

// Form initial values for editing
const editFormInitialValues = computed(() => ({
  name: connectionName.value,
  config: connectionConfig.value,
  auth_policy: connectionAuthPolicy.value,
  has_credentials: hasCredentials.value,
  credentials: {}
}))

function isPasswordField(fieldName: string) {
  const n = (fieldName || '').toLowerCase()
  return n.includes('password') || n.includes('secret') || n.includes('token') || n.includes('key') || n.includes('private')
}

function displayValue(v: any) {
  if (v === undefined || v === null || v === '') return '—'
  return String(v)
}

async function fetchIntegration() {
  if (!dsId.value) return
  const response = await useMyFetch(`/data_sources/${dsId.value}`, { method: 'GET' })
  if ((response.status as any)?.value === 'success') {
    integration.value = (response.data as any)?.value
  }
}

async function fetchFields() {
  const type = connectionType.value
  if (!type) return
  const response = await useMyFetch(`/data_sources/${type}/fields`, { method: 'GET' })
  const schema = (response.data as any)?.value
  if (schema?.config?.properties) {
    configFields.value = Object.entries(schema.config.properties).map(([field_name, s]: any) => ({ field_name, ...s }))
  }
}

function hydrateValues() {
  const config = connectionConfig.value
  if (config) {
    const cfg = typeof config === 'string' ? JSON.parse(config) : config
    Object.keys(cfg || {}).forEach(k => (configFormData[k] = cfg[k]))
  }
}

async function testConnection() {
  if (!dsId.value || isTesting.value) return
  isTesting.value = true
  testConnectionStatus.value = null
  try {
    const response = await useMyFetch(`/data_sources/${dsId.value}/test_connection`, { method: 'GET' })
    testConnectionStatus.value = (response.data as any)?.value || null
  } finally {
    isTesting.value = false
  }
}

function handleEditSuccess() {
  isEditing.value = false
  fetchIntegration()
  fetchFields()
  hydrateValues()
}

function openAddCredentials() {
  showCredsModal.value = true
}

async function disconnectUserCredentials() {
  if (!dsId.value) return
  try {
    await useMyFetch(`/data_sources/${dsId.value}/my-credentials`, { method: 'DELETE' })
    await fetchIntegration()
    hydrateValues()
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
  } finally {
    isTestingUser.value = false
  }
}

onMounted(async () => {
  await fetchIntegration()
  await fetchFields()
  hydrateValues()
})
</script>


