<template>
  <div class="mb-2">
    <div
      class="flex items-center text-xs text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300"
      @click="toggleOpen"
    >
      <span v-if="status === 'running'" class="tool-shimmer inline-flex items-center">
        <Spinner class="w-3 h-3 me-1.5 text-gray-400" />
        {{ t('tools.readInstruction.reading') }}
      </span>
      <template v-else>
        <Icon
          v-if="succeeded"
          :name="open ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
          class="w-3 h-3 me-1 text-gray-400 flex-shrink-0"
        />
        <Icon v-else name="heroicons-x-mark" class="w-3 h-3 me-1.5 text-red-500 flex-shrink-0" />
        <Icon name="heroicons-book-open" class="w-3 h-3 me-1 text-gray-400 flex-shrink-0" />
        <span v-if="succeeded" class="text-gray-700 dark:text-gray-300 truncate">
          {{ t('tools.readInstruction.read') }}<template v-if="title"> · {{ title }}</template>
        </span>
        <span v-else class="text-gray-700 dark:text-gray-300 truncate">
          {{ message || t('tools.readInstruction.failed') }}
        </span>
      </template>
    </div>

    <Transition name="fade">
      <div
        v-if="open && succeeded && text"
        class="mt-1 ms-4 px-2 py-1.5 rounded border border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-gray-900/40 text-[11px] leading-snug text-gray-600 dark:text-gray-400 whitespace-pre-wrap max-h-48 overflow-y-auto"
      >{{ text }}</div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
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

const props = defineProps<{ toolExecution: ToolExecution }>()

const status = computed(() => props.toolExecution?.status || '')
const result = computed<any>(() => props.toolExecution?.result_json || {})
const succeeded = computed(() => status.value === 'success' && result.value?.success !== false)
const title = computed<string>(() => result.value?.title || result.value?.short_id || '')
const text = computed<string>(() => result.value?.text || '')
const message = computed<string>(() => result.value?.message || '')

const open = ref(false)
function toggleOpen() {
  if (succeeded.value) open.value = !open.value
}
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
.fade-enter-active, .fade-leave-active { transition: opacity 0.15s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
</style>
