<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-md' }">
    <div class="p-5">
      <!-- Header -->
      <div class="flex items-center justify-between mb-4">
        <div class="flex items-center gap-3">
          <DataSourceIcon :type="connection?.type" class="h-6 w-6" />
          <div>
            <div class="font-medium text-gray-900">{{ connection?.name }}</div>
            <div class="text-xs text-gray-400">{{ connection?.type }}</div>
          </div>
        </div>
        <button @click="isOpen = false" class="text-gray-400 hover:text-gray-600">
          <UIcon name="heroicons-x-mark" class="w-5 h-5" />
        </button>
      </div>

      <!-- Status & Info -->
      <div class="space-y-3 py-4 border-t border-gray-100">
        <!-- Status -->
        <div class="flex items-center justify-between">
          <span class="text-xs text-gray-500">Status</span>
          <div class="flex items-center gap-2">
            <span :class="['w-2 h-2 rounded-full', isConnected ? 'bg-green-500' : 'bg-red-500']"></span>
            <span class="text-xs text-gray-700">{{ isConnected ? 'Connected' : 'Disconnected' }}</span>
          </div>
        </div>

        <!-- Tables -->
        <div class="flex items-center justify-between">
          <span class="text-xs text-gray-500">Tables</span>
          <span class="text-xs text-gray-700">{{ tableCount }}</span>
        </div>

        <!-- Domains -->
        <div class="flex items-center justify-between">
          <span class="text-xs text-gray-500">Domains</span>
          <span class="text-xs text-gray-700">{{ domainCount }}</span>
        </div>

        <!-- Last Checked -->
        <div class="flex items-center justify-between">
          <span class="text-xs text-gray-500">Last checked</span>
          <span class="text-xs text-gray-700">{{ lastCheckedDisplay || 'Never' }}</span>
        </div>
      </div>

      <!-- Actions -->
      <div class="flex items-center gap-2 pt-4 border-t border-gray-100">
        <button 
          @click="testConnection" 
          :disabled="testing"
          class="flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs text-gray-700 bg-white border border-gray-200 rounded-lg hover:bg-gray-50 disabled:opacity-50"
        >
          <Spinner v-if="testing" class="w-3.5 h-3.5" />
          <UIcon v-else name="heroicons-arrow-path" class="w-3.5 h-3.5" />
          {{ testing ? 'Testing...' : 'Refresh' }}
        </button>
        <!-- Full Edit button (admin with update_data_source permission) -->
        <button 
          v-if="canUpdateDataSource"
          @click="openEdit"
          class="flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs text-gray-700 bg-white border border-gray-200 rounded-lg hover:bg-gray-50"
        >
          <UIcon name="heroicons-pencil" class="w-3.5 h-3.5" />
          Edit
        </button>
        
        <!-- Connect button (user auth required, no admin permission) -->
        <button 
          v-else-if="requiresUserAuth"
          @click="openCredentialsModal"
          class="flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs text-gray-700 bg-white border border-gray-200 rounded-lg hover:bg-gray-50"
        >
          <UIcon name="heroicons-key" class="w-3.5 h-3.5" />
          Connect
        </button>
      </div>

      <!-- Test Result -->
      <div v-if="testResult" class="mt-3 text-xs text-center" :class="testResult.success ? 'text-green-600' : 'text-red-600'">
        {{ testResult.message }}
      </div>

      <!-- Delete Section (only for admins) -->
      <div v-if="canUpdateDataSource" class="pt-4 mt-4 border-t border-gray-100">
        <div v-if="!confirmingDelete">
          <button
            @click="domainCount === 0 ? confirmingDelete = true : null"
            :disabled="domainCount > 0"
            :class="[
              'w-full inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs rounded-lg transition-colors',
              domainCount === 0
                ? 'text-red-600 bg-red-50 border border-red-200 hover:bg-red-100 cursor-pointer'
                : 'text-gray-400 bg-gray-50 border border-gray-200 cursor-not-allowed'
            ]"
          >
            <UIcon name="heroicons-trash" class="w-3.5 h-3.5" />
            Delete Connection
          </button>
          <p v-if="domainCount > 0" class="text-[10px] text-gray-400 text-center mt-1.5">
            Remove all domains first to delete this connection
          </p>
        </div>

        <!-- Confirm delete -->
        <div v-else class="space-y-2">
          <p class="text-xs text-gray-600 text-center">Are you sure? This cannot be undone.</p>
          <div class="flex gap-2">
            <button
              @click="confirmingDelete = false"
              :disabled="deleting"
              class="flex-1 px-3 py-2 text-xs text-gray-600 bg-white border border-gray-200 rounded-lg hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              @click="deleteConnection"
              :disabled="deleting"
              class="flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs text-white bg-red-600 rounded-lg hover:bg-red-700 disabled:opacity-50"
            >
              <Spinner v-if="deleting" class="w-3.5 h-3.5" />
              {{ deleting ? 'Deleting...' : 'Delete' }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </UModal>

  <!-- Edit Connection Modal -->
  <UModal v-model="showEditModal" :ui="{ width: 'sm:max-w-xl' }">
    <UCard>
      <template #header>
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-2">
            <DataSourceIcon :type="connection?.type" class="h-5 w-5" />
            <h3 class="text-lg font-semibold">Edit Connection</h3>
          </div>
          <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark" @click="showEditModal = false" />
        </div>
      </template>

      <div v-if="loadingDetails" class="py-8 text-center">
        <Spinner class="h-5 w-5 mx-auto text-gray-400" />
        <p class="text-sm text-gray-500 mt-2">Loading...</p>
      </div>

      <ConnectForm
        v-else-if="editFormValues"
        mode="edit"
        :initialType="connection?.type"
        :connectionId="connection?.id"
        :initialValues="editFormValues"
        :forceShowSystemCredentials="true"
        :showRequireUserAuthToggle="true"
        :showTestButton="true"
        :showLLMToggle="false"
        :allowNameEdit="true"
        :hideHeader="true"
        @success="handleEditSuccess"
      />
    </UCard>
  </UModal>

  <!-- User Credentials Modal (for users without update permission but require auth) -->
  <UserDataSourceCredentialsModal
    v-model="showCredentialsModal"
    :dataSource="connection"
    @saved="handleCredentialsSaved"
  />
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import ConnectForm from '~/components/datasources/ConnectForm.vue'
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'
import { useCan } from '~/composables/usePermissions'

const props = defineProps<{
  modelValue: boolean
  connection: any
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'updated'): void
}>()

const isOpen = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value)
})

const testing = ref(false)
const testResult = ref<{ success: boolean; message: string } | null>(null)
const showEditModal = ref(false)
const loadingDetails = ref(false)
const connectionDetails = ref<any>(null)
const showCredentialsModal = ref(false)
const confirmingDelete = ref(false)
const deleting = ref(false)

// Permission and auth checks
const canUpdateDataSource = computed(() => useCan('update_data_source'))
const requiresUserAuth = computed(() => props.connection?.auth_policy === 'user_required')

const isConnected = computed(() => {
  // Check multiple possible status fields
  const conn = props.connection
  if (!conn) return false
  
  // Direct status fields
  if (conn.last_status === 'success' || conn.status === 'success') return true
  if (conn.last_status === 'error' || conn.status === 'error') return false
  
  // User status
  const userStatus = conn.user_status?.connection
  if (userStatus === 'success') return true
  if (userStatus === 'error' || userStatus === 'offline') return false
  
  // Default to true if connection exists (assume healthy)
  return true
})

const tableCount = computed(() => props.connection?.table_count || 0)
const domainCount = computed(() => props.connection?.domain_count || 0)

const lastCheckedDisplay = computed(() => {
  const lastChecked = props.connection?.last_checked_at || props.connection?.user_status?.last_checked_at
  if (!lastChecked) return null
  const seconds = Math.floor((Date.now() - new Date(lastChecked).getTime()) / 1000)
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
})

const editFormValues = computed(() => {
  if (!connectionDetails.value) return null
  return {
    name: connectionDetails.value.name,
    config: connectionDetails.value.config || {},
    auth_policy: connectionDetails.value.auth_policy,
    has_credentials: connectionDetails.value.has_credentials,
    credentials: {}
  }
})

async function testConnection() {
  if (!props.connection?.id || testing.value) return
  testing.value = true
  testResult.value = null
  try {
    const { data, error } = await useMyFetch(`/connections/${props.connection.id}/test`, { method: 'POST' })
    if (error.value) {
      testResult.value = { success: false, message: error.value.message || 'Test failed' }
    } else {
      const result = data.value as any
      testResult.value = { 
        success: result.success, 
        message: result.success ? 'Connection successful!' : (result.message || 'Connection failed')
      }
    }
    emit('updated')
  } catch (e: any) {
    testResult.value = { success: false, message: e.message || 'Test failed' }
  } finally {
    testing.value = false
  }
}

async function openEdit() {
  isOpen.value = false
  await nextTick()
  
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

function openCredentialsModal() {
  isOpen.value = false
  showCredentialsModal.value = true
}

function handleCredentialsSaved() {
  emit('updated')
}

async function deleteConnection() {
  if (!props.connection?.id || deleting.value) return
  deleting.value = true
  try {
    const { error } = await useMyFetch(`/connections/${props.connection.id}`, { method: 'DELETE' })
    if (error.value) {
      testResult.value = { success: false, message: error.value.message || 'Delete failed' }
      confirmingDelete.value = false
    } else {
      isOpen.value = false
      emit('updated')
    }
  } catch (e: any) {
    testResult.value = { success: false, message: e.message || 'Delete failed' }
    confirmingDelete.value = false
  } finally {
    deleting.value = false
  }
}

// Reset state when modal closes
watch(isOpen, (val) => {
  if (!val) {
    testResult.value = null
    confirmingDelete.value = false
  }
})
</script>

