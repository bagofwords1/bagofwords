<template>
  <div class="mt-1 text-xs">
    <!-- Running -->
    <span v-if="status === 'running'" class="flex items-center text-gray-500 dark:text-gray-400">
      <Spinner class="w-3 h-3 me-1.5 shrink-0 text-gray-400" />
      <span class="tool-shimmer">{{ title || $t('tools.browser.capturing') }}</span>
    </span>

    <!-- Success: header + inline screenshot (expanded by default) -->
    <template v-else-if="fileId">
      <div class="flex items-center gap-1 text-gray-600 dark:text-gray-300 mb-1.5">
        <Icon name="heroicons-camera" class="w-3.5 h-3.5 text-gray-400" />
        <span class="font-medium truncate">{{ title || $t('tools.browser.screenshot') }}</span>
        <span v-if="displayUrl" dir="ltr" class="ms-1 truncate max-w-[240px] text-gray-400 dark:text-gray-500">{{ displayUrl }}</span>
      </div>
      <AuthenticatedImage
        :file-id="fileId"
        :alt="title || 'Browser screenshot'"
        img-class="max-h-96 w-auto object-contain rounded-lg border border-gray-200 dark:border-gray-700 shadow-sm"
      />
    </template>

    <!-- Error -->
    <div v-else-if="error" class="flex items-center gap-1 text-red-600 dark:text-red-400">
      <Icon name="heroicons-exclamation-triangle" class="w-3.5 h-3.5 shrink-0" />
      <span>{{ error }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import Spinner from '~/components/Spinner.vue'
import AuthenticatedImage from '~/components/AuthenticatedImage.vue'

const props = defineProps<{
  toolExecution: { status: string; result_json?: any; arguments_json?: any }
}>()

const status = computed(() => props.toolExecution?.status || '')
const rj = computed<any>(() => props.toolExecution?.result_json || {})
const args = computed<any>(() => props.toolExecution?.arguments_json || {})
const fileId = computed(() => rj.value.screenshot_file_id || '')
const title = computed(() => args.value.title || '')
const displayUrl = computed(() => rj.value.url || '')
const error = computed(() => rj.value.error_message || '')
</script>

<style scoped>
@keyframes shimmer { 0% { background-position: -100% 0; } 100% { background-position: 100% 0; } }
.tool-shimmer {
  background: linear-gradient(90deg, #888 0%, #999 25%, #ccc 50%, #999 75%, #888 100%);
  background-size: 200% 100%;
  -webkit-background-clip: text; background-clip: text; color: transparent;
  animation: shimmer 2s linear infinite;
}
</style>
