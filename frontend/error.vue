<template>
  <UContainer class="page text-center px-6 py-16">
    <div class="max-w-xl w-full mx-auto">
      <img src="/public/assets/logo-128.png" alt="BOW" class="logo mx-auto mb-8" />

      <div v-if="error?.statusCode === 404" class="space-y-3">
        <h1 class="text-5xl font-semibold tracking-tight">404</h1>
        <p class="text-base text-gray-500">Page not found</p>
        <p class="text-sm text-gray-400">{{ route.fullPath }}</p>
        <div class="mt-6">
          <UButton color="gray" variant="ghost" to="/">Go home</UButton>
        </div>
      </div>

      <div v-else class="space-y-3">
        <h1 class="text-2xl font-semibold tracking-tight">Something went wrong</h1>
        <p class="text-base text-gray-600">{{ error?.message || 'Unknown error' }}</p>
        <div class="mt-6">
          <UButton color="gray" variant="ghost" to="/">Go home</UButton>
        </div>
      </div>
    </div>
  </UContainer>
</template>

<script setup lang="ts">
import type { NuxtError } from '#app'
import { useRoute } from 'vue-router'

const props = defineProps<{ error: NuxtError | null }>()
const error = props.error
const route = useRoute()
</script>

<style scoped>
/* Minimal layout + subtle cursor behavior */
.page {
  min-height: 100vh;
  margin-top: 100px;
  align-items: center;
  justify-content: center;
  cursor: default;
}

.logo {
  width: 48px;
  height: 48px;
}

a, button {
  cursor: pointer;
}
</style>


