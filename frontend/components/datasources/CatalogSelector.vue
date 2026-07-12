<template>
  <div class="p-1">
    <div v-if="loading" class="py-10 text-center text-gray-400 text-sm">Loading…</div>

    <template v-else>
      <!-- File connections → scope view (one card per file connection) -->
      <div v-if="fileConnections.length" class="mb-6">
        <FileSourceScope
          :connections="fileConnections"
          :registry-by-type="registryByType"
          :show-header="true"
          header-title="File sources"
          :show-save="false" />
      </div>

      <!-- SQL / table / object connections → a tables grid each -->
      <div v-for="conn in tableConnections" :key="conn.id" class="mb-6">
        <div class="flex items-center gap-2 mb-2 px-1">
          <DataSourceIcon :type="conn.type" class="w-4 h-4" />
          <span class="text-sm font-medium text-gray-800 dark:text-gray-200">{{ conn.name }}</span>
          <span class="text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500">{{ conn.type }}</span>
        </div>
        <TablesSelector
          :ref="(el) => registerTableRef(conn.id, el)"
          :ds-id="dsId"
          schema="full"
          :connection-filter="conn.id"
          :can-update="canUpdate"
          :show-refresh="true"
          :show-save="false"
          :show-header="false"
          :show-stats="true"
          :item-noun="nounFor(shapeOf(conn))" />
      </div>

      <!-- Tool / MCP connections → the tools picker (per-connection internally) -->
      <div v-if="toolConnections.length" class="mb-6">
        <div class="text-sm font-medium text-gray-800 dark:text-gray-200 mb-2 px-1">Tools</div>
        <ToolsSelector
          :ds-id="dsId"
          :connections="toolConnections"
          :can-update="canUpdate"
          @add-mcp="$emit('add-mcp')"
          @add-custom-api="$emit('add-custom-api')"
          @edit-connection="(c) => $emit('edit-connection', c)"
          @delete-connection="(c) => $emit('delete-connection', c)" />
      </div>

      <div v-if="!fileConnections.length && !tableConnections.length && !toolConnections.length"
           class="py-10 text-center text-sm text-gray-500 dark:text-gray-400">
        No connections attached to this agent yet.
      </div>

      <div v-if="showContinue" class="mt-2 flex justify-end px-1">
        <button type="button" :disabled="saving" @click="saveAndContinue"
                class="px-4 py-2 text-sm font-medium text-white rounded-lg bg-blue-600 hover:bg-blue-700 disabled:bg-gray-400">
          {{ saving ? 'Saving…' : continueLabel }}
        </button>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import DataSourceIcon from '@/components/DataSourceIcon.vue'
import TablesSelector from '@/components/datasources/TablesSelector.vue'
import ToolsSelector from '@/components/datasources/ToolsSelector.vue'
import FileSourceScope from '@/components/datasources/FileSourceScope.vue'

const props = withDefaults(defineProps<{
  dsId: string
  connections: any[]
  registryByType: Record<string, any>
  loading?: boolean
  canUpdate?: boolean
  showContinue?: boolean
  continueLabel?: string
}>(), {
  loading: false,
  canUpdate: true,
  showContinue: false,
  continueLabel: 'Save & Continue',
})
const emit = defineEmits(['saved', 'add-mcp', 'add-custom-api', 'edit-connection', 'delete-connection'])

const shapeOf = (c: any) => props.registryByType[c.type]?.data_shape
  || (c.type === 'mcp' || c.type === 'custom_api' ? 'tools' : 'tables')

const fileConnections = computed(() => (props.connections || []).filter((c) => shapeOf(c) === 'files'))
const toolConnections = computed(() => (props.connections || []).filter((c) => shapeOf(c) === 'tools'))
const tableConnections = computed(() => (props.connections || []).filter((c) => {
  const s = shapeOf(c); return s !== 'files' && s !== 'tools'
}))

function nounFor(shape: string | undefined) {
  if (shape === 'objects') return { sing: 'collection', plural: 'collections' }
  return { sing: 'table', plural: 'tables' }
}

// Collect child TablesSelector instances so one "Save & Continue" persists all.
const tableRefs = new Map<string, any>()
function registerTableRef(id: string, el: any) { if (el) tableRefs.set(id, el); else tableRefs.delete(id) }

const saving = ref(false)
async function saveAndContinue() {
  if (saving.value) return
  saving.value = true
  try {
    for (const el of tableRefs.values()) {
      if (el && typeof el.save === 'function') await el.save()
    }
    emit('saved')
  } finally {
    saving.value = false
  }
}
</script>
