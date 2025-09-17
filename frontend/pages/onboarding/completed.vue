<template>
  <div class="min-h-screen bg-gray-50 flex items-center justify-center py-12 px-4">
    <div class="w-full max-w-6xl">
      <OnboardingView :forceCompleted="true" />
      <div class="text-center mt-6">
        <NuxtLink to="/onboarding/llm" class="text-gray-500 hover:text-gray-700 text-sm">Back to setup</NuxtLink>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'users' })
import OnboardingView from '@/components/onboarding/OnboardingView.vue'
</script>


