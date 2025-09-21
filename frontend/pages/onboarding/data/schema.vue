<template>
  <div class="min-h-screen bg-gray-50 flex items-center justify-center py-12 px-4">
    <div class="w-full max-w-6xl">
      <OnboardingView forcedStepKey="schema_selected" :hideNextButton="true">
        <template #schema>
          <div>
            <div class="">
              <div class="flex items-center justify-between mb-3">
                <input v-model="search" type="text" placeholder="Search tables..." class="border border-gray-300 rounded-lg px-3 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" />
              </div>

              <div v-if="loading" class="text-sm text-gray-500">Loading schema...</div>
              <div v-else>
                <div v-if="filteredTables.length === 0" class="text-sm text-gray-500">No tables found.</div>
                <ul v-else class="divide-y divide-gray-100">
                  <li v-for="table in filteredTables" :key="table.name" class="py-2 flex items-center">
                    <UCheckbox v-model="table.is_active" class="mr-3" />
                    <span class="text-sm text-gray-800">{{ table.name }}</span>
                  </li>
                </ul>

                <div class="mt-4 flex justify-end">
                  <button @click="handleSave" :disabled="saving" class="bg-blue-500 hover:bg-blue-600 text-white text-xs font-medium py-1.5 px-3 rounded disabled:opacity-50">
                    <span v-if="saving">Saving...</span>
                    <span v-else>Save</span>
                  </button>
                </div>
              </div>
            </div>
          </div>
        </template>
      </OnboardingView>
      <div class="text-center mt-6">
        <button @click="skipForNow" class="text-gray-500 hover:text-gray-700 text-sm">Skip onboarding</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'onboarding' })
import OnboardingView from '@/components/onboarding/OnboardingView.vue'

const route = useRoute()
const { updateOnboarding } = useOnboarding()
const router = useRouter()

const dsId = computed(() => String(route.query.ds_id || ''))
const loading = ref(false)
const saving = ref(false)
const tables = ref<any[]>([])
const search = ref('')

const filteredTables = computed(() => {
  if (!search.value) return tables.value
  const q = search.value.toLowerCase()
  return tables.value.filter(t => String(t.name).toLowerCase().includes(q))
})

onMounted(async () => {
  if (!dsId.value) return
  loading.value = true
  try {
    const res = await useMyFetch(`/data_sources/${dsId.value}/schema`, { method: 'GET' })
    tables.value = (res.data as any)?.value || []
  } finally {
    loading.value = false
  }
})

async function handleSave() {
  if (!dsId.value || saving.value) return
  saving.value = true
  try {
    const payload = tables.value.map(t => ({ ...t, datasource_id: dsId.value, pks: t.pks || [], fks: t.fks || [] }))
    const res = await useMyFetch(`/data_sources/${dsId.value}/update_schema`, { method: 'PUT', body: payload })
    if ((res.status as any)?.value === 'success') {
      await updateOnboarding({ current_step: 'instructions_added' as any })
      router.push('/onboarding/context')
    }
  } finally {
    saving.value = false
  }
}

async function skipForNow() { await updateOnboarding({ dismissed: true }); router.push('/') }
</script>


