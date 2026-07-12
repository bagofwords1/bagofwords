<template>

  <div class="min-h-screen py-10 px-4 md:w-2/3 lg:w-1/2 mx-auto text-sm">
      <div class="w-full px-4 ps-0 py-4">
      <div>
        <h1 class="text-lg font-semibold text-center">{{ fileOnly ? 'Confirm file scope' : 'Select tables' }}</h1>
        <p class="mt-4 text-gray-500 dark:text-gray-400 text-center">
          {{ fileOnly
            ? 'File connections are scoped by include-patterns on the connection. Review, then continue.'
            : 'Choose which tables to enable for this agent. You can always change this later.' }}
        </p>
      </div>
        <WizardSteps class="mb-5 mt-4" current="schema" :ds-id="id" />

      <div v-if="!ready" class="py-10 text-center text-gray-400 text-sm">Loading…</div>

      <!-- Structured (SQL / object) agent → the tables grid users like. -->
      <template v-else-if="!fileOnly">
        <div class="bg-white dark:bg-gray-900 rounded-lg">
          <TablesSelector :ds-id="id" schema="full" :connection-filter="tableConnectionIds"
            :can-update="true" :show-refresh="true" :show-save="true" :show-header="true"
            header-title="Select tables" header-subtitle="Choose which tables to enable. Start focused, you can always add more later."
            save-label="Save & Continue" :skip-refresh-on-save="true" @saved="onSaved" />
        </div>
        <p v-if="hasFileConn" class="mt-3 text-xs text-gray-400 text-center">
          This agent also has file sources — manage them in the agent's <strong>Files</strong> tab.
        </p>
      </template>

      <!-- File-only agent → scope confirm, no fake table-picking. -->
      <div v-else class="bg-white dark:bg-gray-900 rounded-lg">
        <FileSourceScope :connections="connections" :registry-by-type="registryByType"
          :show-save="true" save-label="Save & Continue" @saved="onSaved" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true })
import WizardSteps from '@/components/datasources/WizardSteps.vue'
import TablesSelector from '@/components/datasources/TablesSelector.vue'
import FileSourceScope from '@/components/datasources/FileSourceScope.vue'
const route = useRoute()
const router = useRouter()
const id = computed(() => String(route.params.id || ''))

const connections = ref<any[]>([])
const registryByType = ref<Record<string, any>>({})
const ready = ref(false)

const shapeOf = (c: any) => registryByType.value[c.type]?.data_shape
const tableConns = computed(() => connections.value.filter((c: any) => {
  const s = shapeOf(c); return s && s !== 'files' && s !== 'tools'
}))
const hasFileConn = computed(() => connections.value.some((c: any) => shapeOf(c) === 'files'))
const fileOnly = computed(() => tableConns.value.length === 0 && hasFileConn.value)
const tableConnectionIds = computed(() => tableConns.value.map((c: any) => String(c.id)).join(','))

onMounted(async () => {
  try {
    const [reg, conns] = await Promise.all([
      useMyFetch('/available_data_sources', { method: 'GET' }),
      useMyFetch(`/data_sources/${id.value}/connections`, { method: 'GET' }),
    ])
    for (const entry of (reg.data.value as any[]) || []) registryByType.value[entry.type] = entry
    connections.value = (conns.data.value as any[]) || []
  } catch (e) { /* non-fatal */ } finally { ready.value = true }
})

function onSaved() { router.replace(`/agents/new/${id.value}/context`) }
</script>
