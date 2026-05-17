<template>
  <div class="mt-1">
    <Transition name="fade" appear>
      <div class="flex items-center text-xs text-gray-500">
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-globe-alt" class="w-3 h-3 me-1.5 text-gray-400" />
          {{ $t('tools.webFetch.fetching') }}
          <span v-if="displayUrl" dir="ltr" class="ms-1 truncate max-w-[320px] text-gray-500">{{ displayUrl }}</span>
        </span>
        <span v-else-if="isSuccess" class="text-gray-600 flex items-center">
          <Icon name="heroicons-globe-alt" class="w-3 h-3 me-1.5 text-green-500" />
          <span>{{ $t('tools.webFetch.fetched') }}</span>
          <span v-if="displayUrl" dir="ltr" class="ms-1 truncate max-w-[320px] text-gray-600">{{ displayUrl }}</span>
          <span v-if="statusCode" class="ms-1.5 text-[10px] text-gray-400 shrink-0">{{ statusCode }}</span>
        </span>
        <span v-else class="text-gray-600 flex items-center">
          <Icon name="heroicons-globe-alt" class="w-3 h-3 me-1.5 text-orange-500" />
          <span>{{ $t('tools.webFetch.failed') }}</span>
          <span v-if="displayUrl" dir="ltr" class="ms-1 truncate max-w-[320px] text-gray-600">{{ displayUrl }}</span>
        </span>
      </div>
    </Transition>

    <div v-if="!isSuccess && status !== 'running' && errorMessage" class="mt-1 ms-4 text-xs text-gray-500">
      {{ errorMessage }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_json?: any
  arguments_json?: any
}

interface Props {
  toolExecution: ToolExecution
}

const props = defineProps<Props>()

const status = computed<string>(() => props.toolExecution?.status || '')

const result = computed<any>(() => props.toolExecution?.result_json || {})

const isSuccess = computed(() => {
  return status.value === 'success' && result.value?.success === true
})

const displayUrl = computed<string>(() => {
  return result.value?.final_url
    || result.value?.url
    || props.toolExecution?.arguments_json?.url
    || ''
})

const statusCode = computed<number | null>(() => {
  const code = result.value?.status_code
  return typeof code === 'number' ? code : null
})

const errorMessage = computed<string>(() => {
  if (status.value === 'error') {
    return result.value?.error || result.value?.message || t('tools.webFetch.errorOccurred')
  }
  if (status.value === 'success' && result.value?.success === false) {
    return result.value?.error_message || t('tools.webFetch.errorOccurred')
  }
  return ''
})
</script>

<style scoped>
.tool-shimmer {
  animation: shimmer 1.6s linear infinite;
  background: linear-gradient(90deg, rgba(0,0,0,0) 0%, rgba(160,160,160,0.15) 50%, rgba(0,0,0,0) 100%);
  background-size: 300% 100%;
  background-clip: text;
}

@keyframes shimmer {
  0% { background-position: 0% 0; }
  100% { background-position: 100% 0; }
}

.fade-enter-active, .fade-leave-active {
  transition: opacity 0.2s ease;
}
.fade-enter-from, .fade-leave-to {
  opacity: 0;
}
</style>
