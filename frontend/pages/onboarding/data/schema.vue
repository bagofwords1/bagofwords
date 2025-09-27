<template>
  <div class="min-h-screen bg-gray-50 flex items-center justify-center py-12 px-4">
    <div class="w-full max-w-6xl">
      <OnboardingView forcedStepKey="schema_selected" :hideNextButton="true">
        <template #schema>
          <div>
              <div class="">
                <TablesSelector :ds-id="dsId" schema="full" :can-update="true" :show-header="false" :show-refresh="true" :show-save="true" save-label="Save" @saved="onSaved" />
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
import TablesSelector from '@/components/datasources/TablesSelector.vue'

const route = useRoute()
const { updateOnboarding } = useOnboarding()
const router = useRouter()

const dsId = computed(() => String(route.query.ds_id || ''))
function onSaved() { updateOnboarding({ current_step: 'instructions_added' as any }).then(() => router.push('/onboarding/context')) }

async function skipForNow() { await updateOnboarding({ dismissed: true }); router.push('/') }
</script>


