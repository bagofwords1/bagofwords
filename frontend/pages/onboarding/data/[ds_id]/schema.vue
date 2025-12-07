<template>
  <div class="min-h-screen bg-gray-50 flex items-center justify-center py-12 px-4">
    <div class="w-full max-w-6xl">
      <OnboardingView forcedStepKey="schema_selected" :hideNextButton="true">
        <template #schema>
          <div class="relative">
            <TablesSelector
              :dsId="dsId"
              schema="full"
              :canUpdate="true"
              :showRefresh="false"
              :showSave="true"
              saveLabel="Save & Continue"
              maxHeight="50vh"
              @saved="onSaved"
            />
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

async function onSaved() {
  const target = `/onboarding/data/${String(dsId.value)}/context`
  await updateOnboarding({ current_step: 'instructions_added' as any, dismissed: false as any })
  router.replace(target)
}

async function skipForNow() { await updateOnboarding({ dismissed: true }); router.push('/') }
</script>

