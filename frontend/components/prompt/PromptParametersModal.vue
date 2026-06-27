<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-lg' }">
    <div class="p-5">
      <div class="text-sm font-medium text-gray-900 dark:text-white">
        {{ $t('promptParams.title', { title: prompt?.title || $t('promptParams.untitled') }) }}
      </div>
      <div v-if="prompt?.text" class="text-xs text-gray-500 dark:text-gray-400 mt-1 line-clamp-2">{{ prompt.text }}</div>

      <div class="mt-4 space-y-3 max-h-[60vh] overflow-auto pe-1">
        <div v-for="name in paramNames" :key="name">
          <label class="block text-[11px] text-gray-500 dark:text-gray-400 mb-0.5">{{ name }}</label>
          <UInput v-model="values[name]" type="text" size="sm" :placeholder="name" />
        </div>

        <div v-if="paramNames.length === 0" class="text-xs text-gray-500 dark:text-gray-400">
          {{ $t('promptParams.noParams') }}
        </div>
      </div>

      <div class="flex justify-end gap-2 mt-5">
        <button
          @click="onCancel"
          class="px-3 py-1.5 text-xs border border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-300 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800/50"
        >
          {{ $t('promptParams.cancel') }}
        </button>
        <button
          @click="onConfirm"
          :disabled="!canConfirm"
          class="px-3 py-1.5 text-xs border border-blue-300 text-blue-700 rounded-lg hover:bg-blue-50 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {{ $t('promptParams.confirm') }}
        </button>
      </div>
    </div>
  </UModal>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { usePromptFill } from '~/composables/usePromptFill'
import type { PromptParamValue } from '~/composables/usePromptFill'

const props = defineProps<{
  modelValue: boolean
  prompt: any
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'confirm', values: Record<string, PromptParamValue>): void
  (e: 'cancel'): void
}>()

const { extractParamNames } = usePromptFill()

const isOpen = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

// Parameter names are derived from the prompt text's `{{name}}` placeholders —
// there is no separate parameter schema. Each is a simple required text input.
const paramNames = computed<string[]>(() => extractParamNames(props.prompt?.text || ''))

// Scalar values keyed by param name.
const values = ref<Record<string, string>>({})

function seed() {
  const v: Record<string, string> = {}
  for (const name of paramNames.value) v[name] = ''
  values.value = v
}

// Re-seed whenever the modal opens for a (possibly different) prompt.
watch(() => [props.modelValue, props.prompt?.id], ([open]) => {
  if (open) seed()
}, { immediate: true })

// Every placeholder needs a value before the user can confirm.
const canConfirm = computed(() => paramNames.value.every(n => String(values.value[n] ?? '').trim().length > 0))

function collect(): Record<string, PromptParamValue> {
  const out: Record<string, PromptParamValue> = {}
  for (const name of paramNames.value) out[name] = values.value[name]
  return out
}

function onConfirm() {
  if (!canConfirm.value) return
  emit('confirm', collect())
  isOpen.value = false
}

function onCancel() {
  emit('cancel')
  isOpen.value = false
}
</script>
