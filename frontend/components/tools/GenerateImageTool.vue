<script setup lang="ts">
import { computed } from 'vue'
import Spinner from '~/components/Spinner.vue'
import AuthenticatedImage from '~/components/AuthenticatedImage.vue'

const props = defineProps<{
  toolExecution: {
    status: string
    result_json?: any
    arguments_json?: any
  }
}>()

const status = computed(() => props.toolExecution?.status || '')
const rj = computed<any>(() => props.toolExecution?.result_json || {})
const args = computed<any>(() => props.toolExecution?.arguments_json || {})
const fileId = computed(() => rj.value.file_id || '')
const title = computed(() => args.value.title || args.value.prompt || rj.value.filename || 'Generated image')
const error = computed(() => rj.value.error_message || '')
</script>

<template>
  <div class="mt-1 text-xs">
    <!-- Running: spinner + shimmer -->
    <span v-if="status === 'running'" class="flex items-center text-gray-500 dark:text-gray-400">
      <Spinner class="w-3 h-3 me-1.5 shrink-0 text-gray-400" />
      <span class="tool-shimmer">Generating image: {{ title }}…</span>
    </span>

    <!-- Success: header + inline image -->
    <template v-else-if="fileId">
      <div class="flex items-center gap-1 text-gray-600 dark:text-gray-300 mb-1.5">
        <Icon name="heroicons:photo" class="w-3.5 h-3.5 text-gray-400" />
        <span class="font-medium truncate">{{ title }}</span>
      </div>
      <AuthenticatedImage
        :file-id="fileId"
        :alt="title"
        img-class="max-h-80 w-auto object-contain rounded-lg border border-gray-200 dark:border-gray-700 shadow-sm"
      />
    </template>

    <!-- Error -->
    <div v-else-if="error" class="flex items-center gap-1 text-red-600 dark:text-red-400">
      <Icon name="heroicons:exclamation-triangle" class="w-3.5 h-3.5 shrink-0" />
      <span>{{ error }}</span>
    </div>
  </div>
</template>
