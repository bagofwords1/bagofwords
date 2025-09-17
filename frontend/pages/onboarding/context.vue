<template>
  <div class="min-h-screen bg-gray-50 flex items-center justify-center py-12 px-4">
    <div class="w-full max-w-6xl">
      <OnboardingView forcedStepKey="instructions_added" />
      <div class="text-center mt-6">
        <button @click="skipForNow" class="text-gray-500 hover:text-gray-700 text-sm">Skip onboarding</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'users' })
import OnboardingView from '@/components/onboarding/OnboardingView.vue'
const { updateOnboarding } = useOnboarding()
const router = useRouter()
async function skipForNow() { await updateOnboarding({ dismissed: true }); router.push('/') }
</script>


