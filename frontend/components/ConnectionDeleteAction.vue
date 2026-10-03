<template>      <!-- Delete Section (only for admins) -->
      <div v-if="canManage" class="pt-4 mt-4 border-t border-gray-100 dark:border-gray-800">
        <div v-if="!confirmingDelete">
          <button
            @click="confirmingDelete = true"
            :class="compact ? 'inline-flex items-center gap-1.5 text-xs text-red-600 hover:text-red-700' : 'w-full inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs rounded-lg transition-colors text-red-600 bg-red-50 border border-red-200 hover:bg-red-100 cursor-pointer'"
          >
            <UIcon name="heroicons-trash" class="w-3.5 h-3.5" />
            {{ $t('data.deleteConnection') }}
          </button>
        </div>

        <!-- Confirm delete -->
        <div v-else class="space-y-3">
          <!-- Warning for impacted agents -->
          <div v-if="agentCount > 0" class="p-3 bg-amber-50 border border-amber-200 rounded-lg">
            <div class="flex items-start gap-2">
              <UIcon name="heroicons-exclamation-triangle" class="w-4 h-4 text-amber-600 flex-shrink-0 mt-0.5" />
              <div class="text-xs">
                <p class="font-medium text-amber-800">{{ agentCount === 1 ? $t('data.impactAgentsOne', { count: agentCount }) : $t('data.impactAgentsMany', { count: agentCount }) }}</p>
                <p class="text-amber-700 mt-1">
                  {{ agentNames.slice(0, 3).join(', ') }}{{ agentNames.length > 3 ? ' ' + $t('data.andMore', { n: agentNames.length - 3 }) : '' }}
                </p>
                <p class="text-amber-600 mt-1">{{ $t('data.tablesRemovedNote') }}</p>
              </div>
            </div>
          </div>

          <p class="text-xs text-gray-600 dark:text-gray-400 text-center">{{ $t('data.deleteConfirm') }}</p>
          <div class="flex gap-2">
            <button
              @click="confirmingDelete = false"
              :disabled="deleting"
              class="flex-1 px-3 py-2 text-xs text-gray-600 dark:text-gray-400 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800/50"
            >
              {{ $t('data.cancel') }}
            </button>
            <button
              @click="deleteConnection"
              :disabled="deleting"
              class="flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs text-white bg-red-600 rounded-lg hover:bg-red-700 disabled:opacity-50"
            >
              <Spinner v-if="deleting" class="w-3.5 h-3.5" />
              {{ deleting ? $t('data.deleting') : $t('data.delete') }}
            </button>
          </div>
        </div>
      </div></template>
<script setup lang="ts">
import { useCan } from '~/composables/usePermissions'
const props = defineProps<{ connection: any; compact?: boolean }>()
const emit = defineEmits(['deleted'])
const { t } = useI18n()
const toast = useToast()
const canManage = computed(() => useCan('manage_connection', { type: 'connection', id: props.connection?.id }))
const agentCount = computed(() => props.connection?.agent_count || 0)
const agentNames = computed(() => props.connection?.agent_names || [])
const confirmingDelete = ref(false)
const deleting = ref(false)
async function deleteConnection() {
  if (deleting.value) return
  deleting.value = true
  try {
    const { error } = await useMyFetch(`/connections/${props.connection.id}`, { method: 'DELETE' })
    if (error.value) toast.add({ title: t('data.deleteFailed'), description: (error.value as any)?.data?.detail, color: 'red' })
    else emit('deleted')
  } finally { deleting.value = false }
}
</script>