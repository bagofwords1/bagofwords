<template>
  <UPopover :popper="popper">
    <UTooltip :text="label" :popper="{ strategy: 'fixed', placement: 'bottom-start' }">
      <button
        type="button"
        class="rounded-md px-2 py-1 text-xs flex items-center border border-gray-200 dark:border-gray-700"
        :class="modelValue === 'training' ? 'text-sky-600 bg-sky-50 hover:bg-sky-100 border-sky-200' : 'text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white hover:bg-gray-50 dark:hover:bg-gray-800/50'"
      >
        <Icon :name="icon" class="w-4 h-4" />
        <span class="ms-1">{{ label }}</span>
      </button>
    </UTooltip>
    <template #panel="{ close }">
      <div class="p-2 text-xs">
        <div
          class="px-2 py-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800/70 cursor-pointer flex items-center justify-between w-[180px]"
          @click="() => { select('chat'); close(); }"
        >
          <div class="flex items-center">
            <Icon name="heroicons-chat-bubble-left-right" class="w-4 h-4 me-2" />
            {{ $t('prompt.chat') }}
          </div>
          <Icon v-if="modelValue === 'chat'" name="heroicons-check" class="w-4 h-4 text-blue-500" />
        </div>
        <div
          class="px-2 py-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800/70 cursor-pointer flex items-center justify-between"
          @click="() => { select('deep'); close(); }"
        >
          <div class="flex items-center">
            <Icon name="heroicons-light-bulb" class="w-4 h-4 me-2" />
            {{ $t('prompt.deepAnalytics') }}
          </div>
          <Icon v-if="modelValue === 'deep'" name="heroicons-check" class="w-4 h-4 text-blue-500" />
        </div>
        <div
          v-if="canUseTraining"
          class="px-2 py-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800/70 cursor-pointer flex items-center justify-between"
          @click="() => { select('training'); close(); }"
        >
          <div class="flex items-center">
            <Icon name="heroicons-academic-cap" class="w-4 h-4 me-2" />
            {{ $t('prompt.training') }}
          </div>
          <Icon v-if="modelValue === 'training'" name="heroicons-check" class="w-4 h-4 text-blue-500" />
        </div>
      </div>
    </template>
  </UPopover>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  modelValue: 'chat' | 'deep' | 'training' | string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: 'chat' | 'deep' | 'training'): void
}>()

const { t } = useI18n()

const popper = { strategy: 'absolute' as const, placement: 'bottom-start' as const, offset: [0, 8] }

// Mirror PromptBoxV2's training-mode gate.
const { isTrainingModeEnabled } = useOrgSettings()
const canUseTraining = computed(() => useCan('train_mode') && isTrainingModeEnabled.value)

const label = computed(() => {
  switch (props.modelValue) {
    case 'deep': return t('prompt.deepAnalytics')
    case 'training': return t('prompt.training')
    default: return t('prompt.chat')
  }
})

const icon = computed(() => {
  switch (props.modelValue) {
    case 'deep': return 'heroicons-light-bulb'
    case 'training': return 'heroicons-academic-cap'
    default: return 'heroicons-chat-bubble-left-right'
  }
})

function select(m: 'chat' | 'deep' | 'training') {
  emit('update:modelValue', m)
}
</script>
