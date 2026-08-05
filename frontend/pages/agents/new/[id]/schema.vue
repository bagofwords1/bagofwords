<template>
  <div class="min-h-screen py-10 px-4 md:w-2/3 lg:w-1/2 mx-auto text-sm">
    <div class="w-full px-4 ps-0 py-4">
      <div>
        <h1 class="text-lg font-semibold text-center">Configure knowledge</h1>
        <p class="mt-4 text-gray-500 dark:text-gray-400 text-center">
          Pick tables for databases, review the file scope for directories — each source its own way.
        </p>
      </div>
      <WizardSteps class="mb-5 mt-4" current="schema" :ds-id="id" />
      <AgentKnowledgeTabs
        :ds-id="id"
        continue-label="Save & Continue"
        @saved="onSaved"
        @edit-connection="openEditModal"
        @add-mcp="openAddModal('mcp')"
        @add-custom-api="openAddModal('custom_api')"
        @delete-connection="confirmDelete"
      />
    </div>

    <!-- Add / edit a tool-provider connection (pencil and + buttons on the Tools tab). -->
    <AddMCPModal v-if="modalType === 'mcp'" v-model="showModal" :edit-connection="editingConnection" @created="onConnectionChanged" />
    <AddCustomAPIModal v-else-if="modalType === 'custom_api'" v-model="showModal" :edit-connection="editingConnection" @created="onConnectionChanged" />

    <!-- Delete confirmation -->
    <UModal v-model="showDeleteModal" :ui="{ width: 'sm:max-w-sm' }">
      <div class="p-5">
        <h3 class="text-sm font-semibold mb-2">{{ $t('data.deleteConnection') }}</h3>
        <p class="text-xs text-gray-500 dark:text-gray-400 mb-4">{{ deletingConnection?.name }}</p>
        <div class="flex justify-end gap-2">
          <UButton color="gray" variant="ghost" size="sm" @click="showDeleteModal = false">{{ $t('data.cancel') }}</UButton>
          <UButton color="red" size="sm" :loading="deleting" @click="doDelete">{{ $t('data.delete') }}</UButton>
        </div>
      </div>
    </UModal>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true })
import WizardSteps from '@/components/datasources/WizardSteps.vue'
import AgentKnowledgeTabs from '@/components/datasources/AgentKnowledgeTabs.vue'
import AddMCPModal from '~/components/AddMCPModal.vue'
import AddCustomAPIModal from '~/components/AddCustomAPIModal.vue'
const route = useRoute()
const router = useRouter()
const id = computed(() => String(route.params.id || ''))
function onSaved() { router.replace(`/agents/new/${id.value}/context`) }

const showModal = ref(false)
const modalType = ref<'mcp' | 'custom_api' | null>(null)
const editingConnection = ref<any>(null)

function openEditModal(conn: any) {
  editingConnection.value = conn
  modalType.value = conn?.type === 'mcp' ? 'mcp' : 'custom_api'
  showModal.value = true
}
function openAddModal(type: 'mcp' | 'custom_api') {
  editingConnection.value = null
  modalType.value = type
  showModal.value = true
}
function onConnectionChanged() {
  showModal.value = false
  editingConnection.value = null
  // Full route refresh: the tools list, counts and policies all changed.
  router.go(0)
}

const showDeleteModal = ref(false)
const deletingConnection = ref<any>(null)
const deleting = ref(false)
function confirmDelete(conn: any) {
  deletingConnection.value = conn
  showDeleteModal.value = true
}
async function doDelete() {
  if (!deletingConnection.value) return
  deleting.value = true
  try {
    await useMyFetch(`/connections/${deletingConnection.value.id}`, { method: 'DELETE' })
    showDeleteModal.value = false
    router.go(0)
  } finally {
    deleting.value = false
  }
}
</script>
