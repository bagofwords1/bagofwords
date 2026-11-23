<template>      <!-- Delete Section (only for admins) -->
      <div v-if="canManage" class="pt-4 mt-4 border-t border-gray-100 dark:border-gray-800">
        <div v-if="!confirmingDelete">
          <button
            @click="startConfirm"
            :class="compact ? 'inline-flex items-center gap-1.5 text-xs text-red-600 hover:text-red-700' : 'w-full inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs rounded-lg transition-colors text-red-600 dark:text-red-300 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-800 hover:bg-red-100 dark:hover:bg-red-900/50 cursor-pointer'"
          >
            <UIcon name="heroicons-trash" class="w-3.5 h-3.5" />
            {{ $t('data.deleteConnection') }}
          </button>
        </div>

        <!-- Confirm delete -->
        <div v-else class="space-y-3">
          <!-- What the delete affects. Loaded before Delete is offered, so it
               can never be confirmed without seeing the impact. -->
          <div v-if="loadingImpact" class="flex items-center justify-center gap-2 py-2 text-xs text-ink-muted" data-testid="delete-impact-loading">
            <Spinner class="w-3.5 h-3.5" />{{ $t('common.loading') }}
          </div>
          <div v-else-if="impactError" class="p-3 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-800 rounded-lg text-xs text-red-700 dark:text-red-300 flex items-center justify-between gap-2" data-testid="delete-impact-error">
            <span>{{ $t('connectionlessAgent.impactLoadFailed') }}</span>
            <UButton size="xs" color="red" variant="soft" @click="loadImpact">{{ $t('common.retry') }}</UButton>
          </div>
          <div v-else-if="impact && impact.agent_count > 0" class="p-3 bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-800 rounded-lg" data-testid="delete-impact">
            <div class="flex items-start gap-2">
              <UIcon name="heroicons-exclamation-triangle" class="w-4 h-4 text-amber-600 flex-shrink-0 mt-0.5" />
              <div class="text-xs">
                <p class="font-medium text-amber-800">{{ impact.agent_count === 1 ? $t('data.impactAgentsOne', { count: impact.agent_count }) : $t('data.impactAgentsMany', { count: impact.agent_count }) }}</p>
                <template v-if="keptNames.length">
                  <p class="text-amber-700 mt-1" data-testid="delete-kept">
                    {{ keptNames.slice(0, 3).join(', ') }}{{ keptNames.length > 3 ? ' ' + $t('data.andMore', { n: keptNames.length - 3 }) : '' }}
                  </p>
                  <p class="text-amber-600 mt-1">{{ $t('data.tablesRemovedNote') }}</p>
                  <p class="text-amber-600 mt-1" data-testid="delete-keeps-agents">{{ $t('connectionlessAgent.deleteKeepsAgents') }}</p>
                </template>
                <p v-if="impact.connectors_deleted_with_it.length" class="text-red-700 dark:text-red-300 font-medium mt-2" data-testid="delete-removes-connectors">
                  {{ $t('connectionlessAgent.deleteRemovesConnectors', { names: impact.connectors_deleted_with_it.join(', ') }) }}
                </p>
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
              :disabled="deleting || !impact"
              data-testid="delete-connection-confirm"
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

// What deleting this connection does, read from the connection detail every
// time (a connection embedded in an agent carries no counts, and no list
// carries which connectors go with it). Delete stays disabled until it is in.
type Impact = { agent_count: number; agent_names: string[]; connectors_deleted_with_it: string[] }
const impact = ref<Impact | null>(null)
const loadingImpact = ref(false)
const impactError = ref(false)
// Agents are kept; only connectors whose sole connection this is are deleted.
const keptNames = computed(() => {
  const gone = new Set(impact.value?.connectors_deleted_with_it || [])
  return (impact.value?.agent_names || []).filter(n => !gone.has(n))
})
const confirmingDelete = ref(false)
async function loadImpact() {
  if (!props.connection?.id) return
  loadingImpact.value = true
  impactError.value = false
  impact.value = null
  try {
    const { data, error } = await useMyFetch<any>(`/connections/${props.connection.id}`, { method: 'GET' })
    if (error.value || !data.value) { impactError.value = true; return }
    impact.value = {
      agent_count: data.value.agent_count || 0,
      agent_names: data.value.agent_names || [],
      connectors_deleted_with_it: data.value.connectors_deleted_with_it || [],
    }
  } catch {
    impactError.value = true
  } finally {
    loadingImpact.value = false
  }
}
function startConfirm() {
  confirmingDelete.value = true
  loadImpact()
}
const deleting = ref(false)
async function deleteConnection() {
  if (deleting.value || !impact.value) return
  deleting.value = true
  try {
    const { error } = await useMyFetch(`/connections/${props.connection.id}`, { method: 'DELETE' })
    if (error.value) toast.add({ title: t('data.deleteFailed'), description: (error.value as any)?.data?.detail, color: 'red' })
    else emit('deleted')
  } finally { deleting.value = false }
}
</script>
