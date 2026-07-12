<template>

  <div class="min-h-screen py-10 px-4 md:w-2/3 lg:w-1/2 mx-auto text-sm">
      <div class="w-full px-4 ps-0 py-4">
      <div>
        <h1 class="text-lg font-semibold text-center">Configure sources</h1>
        <p class="mt-4 text-gray-500 dark:text-gray-400 text-center">
          Each connection is set up the way that fits it — pick tables for databases,
          review the file scope for directories, choose tools for integrations.
        </p>
      </div>
        <WizardSteps class="mb-5 mt-4" current="schema" :ds-id="id" />

      <div v-if="ready" class="bg-white dark:bg-gray-900 rounded-lg">
        <CatalogSelector
          :ds-id="id"
          :connections="connections"
          :registry-by-type="registryByType"
          :can-update="true"
          :show-continue="true"
          continue-label="Save & Continue"
          @saved="onSaved" />
      </div>
      <div v-else class="py-10 text-center text-gray-400 text-sm">Loading…</div>
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true })
import WizardSteps from '@/components/datasources/WizardSteps.vue'
import CatalogSelector from '@/components/datasources/CatalogSelector.vue'
const route = useRoute()
const router = useRouter()
const id = computed(() => String(route.params.id || ''))

const connections = ref<any[]>([])
const registryByType = ref<Record<string, any>>({})
const ready = ref(false)

onMounted(async () => {
  try {
    const [reg, conns] = await Promise.all([
      useMyFetch('/available_data_sources', { method: 'GET' }),
      useMyFetch(`/data_sources/${id.value}/connections`, { method: 'GET' }),
    ])
    for (const entry of (reg.data.value as any[]) || []) registryByType.value[entry.type] = entry
    connections.value = (conns.data.value as any[]) || []
  } catch (e) {
    // Non-fatal: render with whatever we have.
  } finally {
    ready.value = true
  }
})

function onSaved() { router.replace(`/agents/new/${id.value}/context`) }
</script>
