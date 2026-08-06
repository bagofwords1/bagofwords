<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-4xl' }">
    <div class="p-6">
      <h2 class="text-lg font-semibold mb-4">{{ isEditMode ? $t('data.capiEditTitle') : $t('data.capiConnectTitle') }}</h2>
      <CustomAPIConnectionForm
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
import CustomAPIConnectionForm from '~/components/CustomAPIConnectionForm.vue'

const isOpen = defineModel<boolean>({ default: false })
const props = defineProps<{
  editConnection?: any
  existingConnections?: any[]
}>()
const emit = defineEmits(['created'])

const toast = useToast()
const { t } = useI18n()
const isEditMode = computed(() => !!props.editConnection)

function handleSaved(connection: any) {
  toast.add({ title: isEditMode.value ? t('data.connectionUpdated') : t('data.capiConnected'), color: 'green' })
  isOpen.value = false
  emit('created', connection)
}
</script>
