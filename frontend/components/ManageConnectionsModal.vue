<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-xl' }">
    <UCard>
      <template #header>
        <div class="flex items-center justify-between">
          <h3 class="text-lg font-semibold">Manage Connections</h3>
          <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark" @click="isOpen = false" />
        </div>
      </template>

      <div class="space-y-3">
        <!-- Loading state -->
        <div v-if="loading" class="py-8 text-center">
          <Spinner class="h-5 w-5 mx-auto text-gray-400" />
          <p class="text-sm text-gray-500 mt-2">Loading connections...</p>
        </div>

        <!-- Empty state -->
        <div v-else-if="connections.length === 0" class="py-8 text-center text-gray-500">
          <UIcon name="heroicons-circle-stack" class="w-8 h-8 mx-auto mb-2 text-gray-300" />
          <p class="text-sm">No connections yet.</p>
          <UButton color="primary" variant="soft" size="sm" class="mt-3" @click="navigateTo('/data/new'); isOpen = false">
            Add Connection
          </UButton>
        </div>

        <!-- Connections list -->
        <div v-else class="divide-y divide-gray-100">
          <div 
            v-for="conn in connections" 
            :key="conn.id"
            class="py-3 first:pt-0 last:pb-0"
          >
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-3">
                <DataSourceIcon :type="conn.type" class="h-6" />
                <div>
                  <div class="font-medium text-gray-900">{{ conn.name }}</div>
                  <div class="text-xs text-gray-500">
                    {{ conn.type }} · {{ conn.domain_count }} domain{{ conn.domain_count !== 1 ? 's' : '' }} · {{ conn.table_count }} table{{ conn.table_count !== 1 ? 's' : '' }}
                  </div>
                </div>
              </div>
              <div class="flex items-center gap-1">
                <!-- Test button -->
                <UButton 
                  color="gray" 
                  variant="ghost" 
                  size="xs"
                  :disabled="testingId === conn.id"
                  @click="testConnection(conn)"
                >
                  <Spinner v-if="testingId === conn.id" class="h-3 w-3" />
                  <UIcon v-else name="heroicons-play" class="w-4 h-4" />
                </UButton>

                <!-- Edit button -->
                <UButton 
                  color="gray" 
                  variant="ghost" 
                  size="xs"
                  @click="openEdit(conn)"
                >
                  <UIcon name="heroicons-pencil" class="w-4 h-4" />
                </UButton>
              </div>
            </div>

            <!-- Test result -->
            <div v-if="testResults[conn.id]" class="mt-2 text-xs px-9">
              <span 
                :class="testResults[conn.id].success ? 'text-green-600' : 'text-red-600'"
              >
                {{ testResults[conn.id].message }}
              </span>
            </div>
          </div>
        </div>
      </div>

      <template #footer>
        <div class="flex justify-between">
          <UButton color="primary" variant="soft" @click="navigateTo('/data/new'); isOpen = false">
            <UIcon name="heroicons-plus" class="mr-1" />
            Add Connection
          </UButton>
          <UButton color="gray" variant="ghost" @click="isOpen = false">Close</UButton>
        </div>
      </template>
    </UCard>
  </UModal>

  <!-- Edit Modal -->
  <UModal v-model="showEditModal" :ui="{ width: 'sm:max-w-xl' }">
    <UCard>
      <template #header>
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-2">
            <DataSourceIcon v-if="editingConnection" :type="editingConnection.type" class="h-5" />
            <h3 class="text-lg font-semibold">Edit Connection</h3>
          </div>
          <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark" @click="showEditModal = false" />
        </div>
      </template>

      <div v-if="loadingEditDetails" class="py-8 text-center">
        <Spinner class="h-5 w-5 mx-auto text-gray-400" />
        <p class="text-sm text-gray-500 mt-2">Loading connection details...</p>
      </div>

      <div v-else-if="editingConnection && editFormInitialValues">
        <ConnectForm
          mode="edit"
          :initialType="editingConnection.type"
          :connectionId="editingConnection.id"
          :initialValues="editFormInitialValues"
          :forceShowSystemCredentials="true"
          :showRequireUserAuthToggle="true"
          :showTestButton="true"
          :showLLMToggle="false"
          :allowNameEdit="true"
          :hideHeader="true"
          @success="handleEditSuccess"
        />
      </div>
    </UCard>
  </UModal>

  <!-- Delete Confirmation -->
  <UModal v-model="showDeleteConfirm" :ui="{ width: 'sm:max-w-sm' }">
    <UCard>
      <template #header>
        <h3 class="text-lg font-semibold text-red-600">Delete Connection</h3>
      </template>

      <p class="text-sm text-gray-600">
        Are you sure you want to delete <strong>{{ deletingConnection?.name }}</strong>? This action cannot be undone.
      </p>

      <template #footer>
        <div class="flex justify-end gap-2">
          <UButton color="gray" variant="ghost" @click="showDeleteConfirm = false">Cancel</UButton>
          <UButton color="red" :disabled="deleting" @click="executeDelete">
            <Spinner v-if="deleting" class="h-4 w-4 mr-1" />
            Delete
          </UButton>
        </div>
      </template>
    </UCard>
  </UModal>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import ConnectForm from '~/components/datasources/ConnectForm.vue'

const props = defineProps<{
  modelValue: boolean
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'updated'): void
}>()

const isOpen = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value)
})

const connections = ref<any[]>([])
const loading = ref(false)
const testingId = ref<string | null>(null)
const testResults = ref<Record<string, { success: boolean; message: string }>>({})

// Edit state
const showEditModal = ref(false)
const editingConnection = ref<any>(null)
const loadingEditDetails = ref(false)
const editConnectionDetails = ref<any>(null)

// Computed initial values for ConnectForm in edit mode
const editFormInitialValues = computed(() => {
  if (!editConnectionDetails.value) return null
  return {
    name: editConnectionDetails.value.name,
    config: editConnectionDetails.value.config || {},
    auth_policy: editConnectionDetails.value.auth_policy,
    has_credentials: editConnectionDetails.value.has_credentials,
    credentials: {} // Credentials are never returned from backend for security
  }
})

// Delete state
const showDeleteConfirm = ref(false)
const deletingConnection = ref<any>(null)
const deleting = ref(false)

async function fetchConnections() {
  loading.value = true
  try {
    const { data } = await useMyFetch('/connections', { method: 'GET' })
    if (data.value) {
      connections.value = data.value as any[]
    }
  } finally {
    loading.value = false
  }
}

async function testConnection(conn: any) {
  testingId.value = conn.id
  testResults.value[conn.id] = { success: false, message: 'Testing...' }
  try {
    const { data, error } = await useMyFetch(`/connections/${conn.id}/test`, { method: 'POST' })
    if (error.value) {
      testResults.value[conn.id] = { success: false, message: error.value.message || 'Test failed' }
    } else {
      const result = data.value as any
      testResults.value[conn.id] = { 
        success: result.success, 
        message: result.success ? 'Connection successful!' : (result.message || 'Connection failed')
      }
    }
  } catch (e: any) {
    testResults.value[conn.id] = { success: false, message: e.message || 'Test failed' }
  } finally {
    testingId.value = null
  }
}

async function openEdit(conn: any) {
  editingConnection.value = conn
  editConnectionDetails.value = null
  
  // Close the parent modal first to avoid nested modal issues
  isOpen.value = false
  
  // Small delay to ensure parent modal is closed before opening edit modal
  await nextTick()
  showEditModal.value = true
  
  // Load full connection details
  loadingEditDetails.value = true
  try {
    const { data: connData } = await useMyFetch(`/connections/${conn.id}`, { method: 'GET' })
    if (connData.value) {
      editConnectionDetails.value = connData.value as any
      editingConnection.value = { ...conn, ...(connData.value as any) }
    }
  } finally {
    loadingEditDetails.value = false
  }
}

async function handleEditSuccess() {
  // Clear editing connection first to prevent watch from reopening
  editingConnection.value = null
  showEditModal.value = false
  await fetchConnections()
  emit('updated')
  // Reopen the parent modal after editing
  await nextTick()
  isOpen.value = true
}

function confirmDelete(conn: any) {
  deletingConnection.value = conn
  showDeleteConfirm.value = true
}

async function executeDelete() {
  if (!deletingConnection.value) return
  deleting.value = true
  try {
    await useMyFetch(`/connections/${deletingConnection.value.id}`, { method: 'DELETE' })
    showDeleteConfirm.value = false
    await fetchConnections()
    emit('updated')
  } finally {
    deleting.value = false
  }
}

watch(isOpen, (newVal) => {
  if (newVal) {
    fetchConnections()
    testResults.value = {}
  }
})

// When edit modal is closed (via X button), reopen the parent modal
watch(showEditModal, async (newVal) => {
  if (!newVal && editingConnection.value) {
    // Small delay to avoid modal conflicts
    await nextTick()
    isOpen.value = true
    editingConnection.value = null
  }
})
</script>

