<template>

  <div class="min-h-screen py-10 px-4 md:w-1/2 mx-auto text-sm">
      <div class="w-full px-4 ps-0 py-4">
      <div>
        <h1 class="text-lg font-semibold text-center">{{ isFileShaped ? 'Confirm file scope' : 'Select Tables' }}</h1>
        <p class="mt-4 text-gray-500 dark:text-gray-400 text-center">
          {{ isFileShaped
            ? 'File connections are scoped by include-patterns on the connection — not by picking individual files. Review the scope, then continue.'
            : 'Choose 5-20 related tables for this agent. You can always add more later.' }}
        </p>
      </div>
        <WizardSteps class="mb-5 mt-4" current="schema" :ds-id="id" />

      <!-- File-shaped agents: scope summary instead of the tables grid. -->
      <div v-if="ready && isFileShaped" class="bg-white dark:bg-gray-900 rounded-lg">
        <FileSourceScope
          :connections="connections"
          :registry-by-type="registryByType"
          :loading="!ready"
          :show-save="true"
          save-label="Save & Continue"
          @saved="onSaved" />
      </div>

      <!-- SQL / table-shaped agents: unchanged tables selector. -->
      <div v-else-if="ready" class="bg-white dark:bg-gray-900 rounded-lg">
        <TablesSelector :ds-id="id" schema="full" :can-update="true" :show-refresh="true" :show-save="true" :show-header="true" header-title="Select tables" header-subtitle="Choose 5-20 related tables. Start focused, you can always add more later." save-label="Save & Continue" :skip-refresh-on-save="true" @saved="onSaved" />
      </div>

      <div v-else class="py-10 text-center text-gray-400 text-sm">Loading…</div>
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

// Agent is "file-shaped" when every attached connection's data_shape is
// 'files'. Mixed / SQL agents keep the tables grid.
const isFileShaped = computed(() => {
  const shapes = new Set(
    connections.value.map((c: any) => registryByType.value[c.type]?.data_shape).filter(Boolean)
  )
  return shapes.size >= 1 && Array.from(shapes).every((s) => s === 'files')
})

onMounted(async () => {
  try {
    const [reg, conns] = await Promise.all([
      useMyFetch('/available_data_sources', { method: 'GET' }),
      useMyFetch(`/data_sources/${id.value}/connections`, { method: 'GET' }),
    ])
    for (const entry of (reg.data.value as any[]) || []) registryByType.value[entry.type] = entry
    connections.value = (conns.data.value as any[]) || []
  } catch (e) {
    // On any failure, fall back to the tables selector (safe default).
  } finally {
    ready.value = true
  }
})

function onSaved() { router.replace(`/agents/new/${id.value}/context`) }
</script>
