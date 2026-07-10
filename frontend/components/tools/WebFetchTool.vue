<template>
  <div class="mt-1">
    <Transition name="fade" appear>
      <div class="flex items-center text-xs text-gray-500 dark:text-gray-400">
        <span v-if="status === 'running'" class="flex items-center">
          <Spinner class="w-3 h-3 me-1.5 shrink-0 text-gray-400" />
          <span class="tool-shimmer">
            <template v-if="modelTitle">{{ modelTitle }}</template>
            <template v-else>{{ $t('tools.webFetch.fetching') }}<span v-if="displayUrl" dir="ltr" class="ms-1">{{ displayUrl }}</span></template>
          </span>
        </span>
        <span v-else-if="isSuccess" class="text-gray-600 dark:text-gray-400 flex items-center">
          <Icon name="heroicons-globe-alt" class="w-3 h-3 me-1.5 text-green-500" />
          <span>{{ modelTitle || $t('tools.webFetch.fetched') }}</span>
          <span v-if="displayUrl && !modelTitle" dir="ltr" class="ms-1 truncate max-w-[320px] text-gray-600 dark:text-gray-400">{{ displayUrl }}</span>
          <span v-if="statusCode" class="ms-1.5 text-[10px] text-gray-400 shrink-0">{{ statusCode }}</span>
        </span>
        <span v-else class="text-gray-600 dark:text-gray-400 flex items-center">
          <Icon name="heroicons-globe-alt" class="w-3 h-3 me-1.5 text-orange-500" />
          <span>{{ $t('tools.webFetch.failed') }}</span>
          <span v-if="displayUrl" dir="ltr" class="ms-1 truncate max-w-[320px] text-gray-600 dark:text-gray-400">{{ displayUrl }}</span>
        </span>
      </div>
    </Transition>

    <div v-if="!isSuccess && status !== 'running' && errorMessage" class="mt-1 ms-4 text-xs text-gray-500 dark:text-gray-400">
      {{ errorMessage }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import Spinner from '~/components/Spinner.vue'

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

const modelTitle = computed<string>(() => {
  const t = props.toolExecution?.arguments_json?.title
  return typeof t === 'string' && t.trim() ? t.trim() : ''
})

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
@keyframes shimmer { 0% { background-position: -100% 0; } 100% { background-position: 100% 0; } }
.tool-shimmer {
  background: linear-gradient(90deg, #888 0%, #999 25%, #ccc 50%, #999 75%, #888 100%);
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: shimmer 2s linear infinite;
}

.fade-enter-active, .fade-leave-active {
  transition: opacity 0.2s ease;
}
.fade-enter-from, .fade-leave-to {
  opacity: 0;
}
</style>
