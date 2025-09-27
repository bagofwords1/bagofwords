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



              <TablesSelector :ds-id="dsId" schema="user" :can-update="true" :show-refresh="true" :show-save="true" save-label="Save" @saved="onSaved" />

  

          </div>
            <div class="mt-3 flex justify-end">
              <button @click="handleSave" :disabled="saving" class="bg-blue-500 hover:bg-blue-600 text-white text-xs font-medium py-1.5 px-3 rounded disabled:opacity-50">
                <span v-if="saving">Saving...</span>
                <span v-else>Save</span>
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
import TablesSelector from '@/components/datasources/TablesSelector.vue'

const route = useRoute()
const { updateOnboarding } = useOnboarding()
const router = useRouter()

const dsId = computed(() => String(route.params.ds_id || ''))
function onSaved() {
  const target = `/onboarding/data/${String(dsId.value)}/context`
  updateOnboarding({ current_step: 'instructions_added' as any, dismissed: false as any }).then(() => router.replace(target))
}

async function skipForNow() { await updateOnboarding({ dismissed: true }); router.push('/') }
</script>


