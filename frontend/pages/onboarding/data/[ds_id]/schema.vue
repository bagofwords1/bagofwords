<template>
  <div class="min-h-screen bg-gray-50 flex items-center justify-center py-12 px-4">
    <div class="w-full max-w-6xl">
      <OnboardingView forcedStepKey="schema_selected" :hideNextButton="true">
        <template #schema>
          <div class="relative h-[50vh]">
             <!-- Schema card -->
             <div
               class="bg-white border border-gray-200 rounded-lg p-4 flex flex-col h-full overflow-hidden"
             >
              <div class="mb-2">
                <input v-model="search" type="text" placeholder="Search tables..." class="border border-gray-300 rounded-lg px-3 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" />
                <div class="mt-1 text-xs text-gray-500 text-right">{{ selectedCount }} of {{ totalTables }} selected</div>
              </div>



              <div v-if="loading" class="text-sm text-gray-500">Loading schema...</div>
              <div v-else class="flex-1 flex flex-col h-full">
                <div v-if="filteredTables.length === 0" class="text-sm text-gray-500">No tables found.</div>
                <div v-else class="flex-1 flex flex-col min-h-full">
                <div class="flex-1 overflow-y-auto min-h-0 pr-1">
                    <ul class="divide-y divide-gray-100">
                      <li v-for="table in filteredTables" :key="table.name" class="py-2 px-4">
                        <div class="flex items-center">
                          <UCheckbox color="blue" v-model="table.is_active" class="mr-3" />
                          <button type="button" class="flex items-center text-left flex-1" @click="toggleTable(table)">
                            <UIcon :name="expandedTables[table.name] ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-4 h-4 mr-1 text-gray-500" />
                            <span class="text-sm text-gray-800">{{ table.name }}</span>
                          </button>
                        </div>
                        <div v-if="expandedTables[table.name] && table.columns" class="mt-2 ml-7">
                          <div class="border border-gray-100 rounded">
                            <div class="grid grid-cols-2 text-xs font-medium text-gray-500 bg-gray-50 px-2 py-1 rounded-t">
                              <div>Name</div>
                              <div>Type</div>
                            </div>
                            <div class="divide-y divide-gray-100">
                              <div v-for="col in table.columns" :key="col.name" class="grid grid-cols-2 text-xs px-2 py-1">
                                <div class="text-gray-700">{{ col.name }}</div>
                                <div class="text-gray-500">{{ col.dtype || col.type }}</div>
                              </div>
                            </div>
                          </div>
                        </div>
                      </li>
                    </ul>
                </div>
              </div>
            </div>

  

          </div>
            <div class="mt-3 flex justify-end">
              <button @click="handleSave" :disabled="saving" class="bg-blue-500 hover:bg-blue-600 text-white text-xs font-medium py-1.5 px-3 rounded disabled:opacity-50">
                <span v-if="saving">Saving...</span>
                <span v-else>Save & Continue</span>
              </button>
            </div>
          </div>
        </template>
      </OnboardingView>
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'onboarding' })
import OnboardingView from '@/components/onboarding/OnboardingView.vue'
import Spinner from '@/components/Spinner.vue'

const route = useRoute()
const { updateOnboarding } = useOnboarding()
const router = useRouter()

const dsId = computed(() => String(route.params.ds_id || ''))
const loading = ref(false)
const saving = ref(false)
const tables = ref<any[]>([])
const search = ref('')
const expandedTables = ref<Record<string, boolean>>({})

const totalTables = computed(() => tables.value.length)
const selectedCount = computed(() => tables.value.filter(t => !!t.is_active).length)

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

function toggleTable(table: any) {
  const current = expandedTables.value[table.name]
  expandedTables.value[table.name] = !current
}

async function handleSave() {
  if (!dsId.value || saving.value) return
  saving.value = true
  try {
    const payload = tables.value.map(t => ({ ...t, datasource_id: dsId.value, pks: t.pks || [], fks: t.fks || [] }))
    const res = await useMyFetch(`/data_sources/${dsId.value}/update_schema`, { method: 'PUT', body: payload })
    if ((res.status as any)?.value === 'success') {
      const target = `/onboarding/data/${String(dsId.value)}/context`
      await updateOnboarding({ current_step: 'instructions_added' as any, dismissed: false as any })
      router.replace(target)
    }
  } finally {
    saving.value = false
  }
}

async function skipForNow() { await updateOnboarding({ dismissed: true }); router.push('/') }
</script>

