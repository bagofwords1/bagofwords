<template>
  <div class="loading-state flex items-center justify-center p-8">
    <div class="text-center">
      <div class="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600 mb-4"></div>
      <p class="text-gray-600 text-sm">{{ message }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
interface Props {
  message?: string
}

withDefaults(defineProps<Props>(), {
  message: 'Loading...'
})
</script>

<style scoped>
.loading-state {
  min-height: 200px;
}
</style>