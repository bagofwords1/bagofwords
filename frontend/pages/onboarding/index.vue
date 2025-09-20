<template>
  <div class="min-h-screen bg-gray-50 flex items-center justify-center py-12 px-4">
    <div class="w-full max-w-6xl relative">
      <Transition name="fade" mode="out-in">
        <div v-if="loading" key="loading" class="flex items-center justify-center h-40">
          <div class="flex items-center text-gray-500 text-sm">
            <Spinner class="w-4 h-4 mr-2" />
            Loading...
          </div>
        </div>
        <div v-else key="content">
          <OnboardingView forcedStepKey="onboarding" :hideNextButton="false" :hideSidebar="true">
            <template #onboarding>
              <div>
                <div class="mb-5">
                    <p class="text-sm text-gray-600 mb-5">
                      Getting started is quick: just connect your model and your data source, and you’ll be ready to ask your first question.
                    </p>
                  <p class="text-sm text-gray-600">
                    👉 Let’s set things up!
                  </p>
                </div>
              </div>

            </template>

          </OnboardingView>
          <div class="text-center">
            <button @click="skipForNow" class="text-gray-500 hover:text-gray-700 text-sm inline text-sm mt-4 rounded-md px-3 py-1.5">Skip onboarding</button>
          </div>
        </div>
      </Transition>
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'users' })
import OnboardingView from '@/components/onboarding/OnboardingView.vue'
import Spinner from '@/components/Spinner.vue'
const { updateOnboarding } = useOnboarding()
const router = useRouter()
const { fetchOnboarding, onboarding } = useOnboarding()
const loading = ref(true)
onMounted(async () => {
  try {
    await fetchOnboarding()
  } finally {
    loading.value = false
  }
})
async function skipForNow() { await updateOnboarding({ dismissed: true }); router.push('/') }
</script>


