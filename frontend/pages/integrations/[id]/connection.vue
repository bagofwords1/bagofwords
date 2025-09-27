<template>
    <div class="py-6">
        <div class="bg-white border border-gray-200 rounded-lg p-6">
            <div class="flex items-center justify-start space-x-3">
                <h2 class="text-lg font-semibold text-gray-900">Connection</h2>
                <div v-if="canUpdate">
                    <button @click="openEdit" class="text-xs text-blue-600 hover:underline">Edit</button>
                </div>
            </div>

            <div v-if="integration" class="mt-6">
                <!-- Configuration (main fields) -->
                <div class="mb-6">
                    <h3 class="text-sm font-semibold text-gray-800 mb-3">Configuration</h3>
                    <div v-if="configFields.length === 0" class="text-xs text-gray-500">No configuration fields.</div>
                    <div v-else class="space-y-3">
                        <div v-for="field in configFields" :key="field.field_name">
                            <label class="block text-xs font-medium text-gray-600 mb-1">{{ field.title || field.field_name }}</label>
                            <div class="bg-gray-50 border border-gray-200 rounded-md p-2 text-sm text-gray-800">
                                <span v-if="field.type === 'boolean'">{{ configFormData[field.field_name] ? 'Yes' : 'No' }}</span>
                                <span v-else>{{ displayValue(configFormData[field.field_name]) }}</span>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- User Connection (only for user_required auth) -->
                <div class="mb-6" v-if="integration?.auth_policy === 'user_required'">
                    <!-- Admin control row above title -->
                    <div v-if="isAdmin" class="mb-2 flex items-center space-x-3 text-sm text-gray-800">
                        <UButton size="xs" color="gray" :loading="isTesting" @click="testConnection">Test connection</UButton>
                        <UButton v-if="canUpdate" size="xs" color="gray" @click="openEdit">Edit connection</UButton>
                    </div>
                    <h3 class="text-sm font-semibold text-gray-800 mb-3 mt-5">User Connection</h3>
                    <div v-if="isAdmin" class="text-xs text-gray-800 mb-3">Admin users will assume system credentials</div>
    
                    <div class="text-sm text-gray-800 flex items-center space-x-3">
                        <template v-if="integration?.user_status?.has_user_credentials">
                            <span class="inline-flex items-center text-green-700">
                                <UIcon name="heroicons-check-circle" class="w-4 h-4 mr-1" />
                                Connected as {{ connectedUserDisplay }}
                            </span>
                            <UButton v-if="!isAdmin" size="xs" color="gray" :loading="isTestingUser" @click="testUserConnection">Test connection</UButton>
                            <UButton size="xs" color="red" variant="soft" @click="disconnectUserCredentials">Disconnect</UButton>
                        </template>
                        <template v-else>
                            <template v-if="!isAdmin">
                                <span class="inline-flex items-center text-gray-600">
                                    <UIcon name="heroicons-exclamation-circle" class="w-4 h-4 mr-1" />
                                    Not connected
                                </span>
                                <UButton size="xs" color="blue" variant="solid" @click="openAddCredentials">Add</UButton>
                            </template>
                            <template v-else>
                                <!-- Admin sees controls above; nothing here -->
                            </template>
                        </template>
                    </div>
                    <div v-if="(!isAdmin && integration?.user_status?.has_user_credentials && testUserStatus !== null) || (isAdmin && testConnectionStatus !== null)" class="mt-2 text-sm">
                        <template v-if="!isAdmin">
                            <span v-if="testUserStatus?.success" class="inline-flex items-center text-green-700">
                                <UIcon name="heroicons-check-circle" class="w-4 h-4 mr-1" /> Connected
                            </span>
                            <span v-else class="inline-flex items-center text-red-700">
                                <UIcon name="heroicons-x-circle" class="w-4 h-4 mr-1" /> Failed
                            </span>
                            <div v-if="!testUserStatus?.success && testUserStatus?.message" class="mt-1 text-xs text-gray-600">
                                {{ testUserStatus.message }}
                            </div>
                        </template>
                        <template v-else>
                            <span v-if="testConnectionStatus?.success" class="inline-flex items-center text-green-700">
                                <UIcon name="heroicons-check-circle" class="w-4 h-4 mr-1" /> Connected
                            </span>
                            <span v-else class="inline-flex items-center text-red-700">
                                <UIcon name="heroicons-x-circle" class="w-4 h-4 mr-1" /> Failed
                            </span>
                        </template>
                    </div>
                </div>

                <!-- Credentials section removed per requirements -->
            </div>

            <div v-else class="text-sm text-gray-500 mt-6">Loading...</div>

            <!-- Footer actions: bottom-left -->
            <div class="mt-6 flex items-center space-x-3" v-if="showFooterTest">
                <span v-if="testConnectionStatus !== null" class="text-sm">
                    <span v-if="testConnectionStatus?.success" class="inline-flex items-center text-green-700">
                        <UIcon name="heroicons-check-circle" class="w-4 h-4 mr-1" /> Connected
                    </span>
                    <span v-else class="inline-flex items-center text-red-700">
                        <UIcon name="heroicons-x-circle" class="w-4 h-4 mr-1" /> Failed
                    </span>
                </span>
            </div>
        </div>
    </div>

  <UModal v-model="showEdit" :ui="{ width: 'sm:max-w-2xl' }">
    <div class="p-4">
      <ConnectForm
        mode="edit"
        :data-source-id="dsId"
        :initial-type="integration?.type"
        :initial-values="{ name: integration?.name, type: integration?.type, config: integration?.config, credentials: integration?.credentials, is_public: integration?.is_public, auth_policy: integration?.auth_policy }"
              :show-test-button="true"
              :show-llm-toggle="false"
              :allow-name-edit="false"
              :force-show-system-credentials="true"
        @success="(updated) => { showEdit = false; fetchIntegration(); fetchFields(); hydrateValues(); }"
      />
    </div>
  </UModal>

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
const credentialFields = ref<any[]>([])
const configFormData = reactive<any>({})
const credentialsFormData = reactive<any>({})

const isTesting = ref(false)
const testConnectionStatus = ref<any>(null)
const showEdit = ref(false)
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

const showFooterTest = computed(() => {
  // Hide footer test when admin on user_required, as the inline section provides the test
  return !(integration.value?.auth_policy === 'user_required' && isAdmin.value)
})

function isPasswordField(fieldName: string) {
  const n = (fieldName || '').toLowerCase()
  return n.includes('password') || n.includes('secret') || n.includes('token') || n.includes('key')
}

function displayValue(v: any) {
  if (v === undefined || v === null || v === '') return 'Not configured'
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
  if (!integration.value?.type) return
  const response = await useMyFetch(`/data_sources/${integration.value.type}/fields`, { method: 'GET' })
  const schema = (response.data as any)?.value
  if (schema?.config?.properties) {
    configFields.value = Object.entries(schema.config.properties).map(([field_name, s]: any) => ({ field_name, ...s }))
  }
  if (schema?.credentials?.properties) {
    credentialFields.value = Object.entries(schema.credentials.properties).map(([field_name, s]: any) => ({ field_name, ...s }))
  }
}

function hydrateValues() {
  if (integration.value?.config) {
    const cfg = typeof integration.value.config === 'string' ? JSON.parse(integration.value.config) : integration.value.config
    Object.keys(cfg || {}).forEach(k => (configFormData[k] = cfg[k]))
  }
  if (integration.value?.credentials) {
    const creds = typeof integration.value.credentials === 'string' ? JSON.parse(integration.value.credentials) : integration.value.credentials
    Object.keys(creds || {}).forEach(k => (credentialsFormData[k] = creds[k]))
  }
}

async function testConnection() {
  if (!dsId.value || isTesting.value) return
  isTesting.value = true
  try {
    const response = await useMyFetch(`/data_sources/${dsId.value}/test_connection`, { method: 'GET' })
    testConnectionStatus.value = (response.data as any)?.value || null
  } finally {
    isTesting.value = false
  }
}

function openEdit() {
  showEdit.value = true
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
    // no-op; errors handled by fetch utility
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
    // Invoke test for the existing data source. Backend will use stored user creds per policy
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


