<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-lg' }">
    <div class="p-6">
      <h2 class="text-lg font-semibold mb-4">{{ isEditMode ? $t('settings.mcpModal.editTitle') : $t('settings.mcpModal.connectTitle') }}</h2>
      <MCPConnectionForm
        v-if="isOpen"
        :editConnection="editConnection"
        :existingConnections="existingConnections"
        @saved="handleSaved"
        @cancel="isOpen = false"
      />
    </div>
  </UModal>
</template>

<script setup lang="ts">
import MCPConnectionForm from '~/components/MCPConnectionForm.vue'

const { t } = useI18n()
const isOpen = defineModel<boolean>({ default: false })
const props = defineProps<{
  editConnection?: any
  existingConnections?: any[]
}>()
const emit = defineEmits(['created'])

const toast = useToast()
const isEditMode = computed(() => !!props.editConnection)

function handleSaved(connection: any) {
  toast.add({ title: isEditMode.value ? t('settings.mcpModal.updated') : t('settings.mcpModal.connected'), color: 'green' })
  isOpen.value = false
  emit('created', connection)
}
</script>
