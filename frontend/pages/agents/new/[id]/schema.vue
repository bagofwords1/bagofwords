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

      <div v-if="!ready" class="py-10 text-center text-gray-400 text-sm">Loading…</div>

      <div v-else>
        <!-- Category tabs — only the ones this agent has (Files is always available). -->
        <div v-if="tabs.length > 1" class="flex gap-1 border-b border-gray-200 dark:border-gray-700 mb-4">
          <button v-for="tab in tabs" :key="tab.key" type="button"
            class="px-3 py-2 text-sm font-medium -mb-px border-b-2 transition-colors"
            :class="active === tab.key ? 'border-blue-600 text-blue-600' : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400'"
            @click="active = tab.key">{{ tab.label }}</button>
        </div>

        <div v-show="active === 'tables'" class="bg-white dark:bg-gray-900 rounded-lg">
          <TablesSelector ref="tablesRef" :ds-id="id" schema="full" :connection-filter="tableConnectionIds"
            :can-update="true" :show-refresh="true" :show-save="false" :show-header="true"
            header-title="Select tables" header-subtitle="Choose which tables to enable. Start focused, you can always add more later."
            :show-stats="true" :skip-refresh-on-save="true" />
        </div>

        <div v-show="active === 'files'" class="bg-white dark:bg-gray-900 rounded-lg px-1">
          <AgentFilesPanel :ds-id="id" :can-update="true" />
        </div>

        <div v-show="active === 'tools'" class="bg-white dark:bg-gray-900 rounded-lg">
          <ToolsSelector :ds-id="id" :connections="toolConns" :can-update="true" />
        </div>

        <div class="mt-4 flex justify-end">
          <button type="button" :disabled="saving" @click="saveAndContinue"
            class="px-4 py-2 text-sm font-medium text-white rounded-lg bg-blue-600 hover:bg-blue-700 disabled:bg-gray-400">
            {{ saving ? 'Saving…' : 'Save & Continue' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true })
import WizardSteps from '@/components/datasources/WizardSteps.vue'
import TablesSelector from '@/components/datasources/TablesSelector.vue'
import AgentFilesPanel from '@/components/datasources/AgentFilesPanel.vue'
import ToolsSelector from '@/components/datasources/ToolsSelector.vue'
const route = useRoute()
const router = useRouter()
const id = computed(() => String(route.params.id || ''))

const connections = ref<any[]>([])
const registryByType = ref<Record<string, any>>({})
const ready = ref(false)
const active = ref<'tables' | 'files' | 'tools'>('files')
const tablesRef = ref<any>(null)
const saving = ref(false)

const shapeOf = (c: any) => registryByType.value[c.type]?.data_shape
  || (c.type === 'mcp' || c.type === 'custom_api' ? 'tools' : undefined)
const tableConns = computed(() => connections.value.filter((c: any) => { const s = shapeOf(c); return s === 'tables' || s === 'objects' }))
const fileConns = computed(() => connections.value.filter((c: any) => shapeOf(c) === 'files'))
const toolConns = computed(() => connections.value.filter((c: any) => shapeOf(c) === 'tools'))
const tableConnectionIds = computed(() => tableConns.value.map((c: any) => String(c.id)).join(','))

// Files tab is always available (uploads are agent-level). Tables/Tools only
// when the agent has such connections.
const tabs = computed(() => {
  const out: { key: 'tables' | 'files' | 'tools'; label: string }[] = []
  if (tableConns.value.length) out.push({ key: 'tables', label: 'Tables' })
  out.push({ key: 'files', label: 'Files' })
  if (toolConns.value.length) out.push({ key: 'tools', label: 'Tools' })
  return out
})

onMounted(async () => {
  try {
    const [reg, conns] = await Promise.all([
      useMyFetch('/available_data_sources', { method: 'GET' }),
      useMyFetch(`/data_sources/${id.value}/connections`, { method: 'GET' }),
    ])
    for (const entry of (reg.data.value as any[]) || []) registryByType.value[entry.type] = entry
    connections.value = (conns.data.value as any[]) || []
  } catch (e) { /* non-fatal */ } finally {
    ready.value = true
    active.value = tabs.value[0]?.key || 'files'
  }
})

async function saveAndContinue() {
  if (saving.value) return
  saving.value = true
  try {
    if (tablesRef.value && typeof tablesRef.value.save === 'function') await tablesRef.value.save()
    router.replace(`/agents/new/${id.value}/context`)
  } finally { saving.value = false }
}
</script>
