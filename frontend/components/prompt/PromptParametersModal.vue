<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-lg' }">
    <div class="p-5">
      <div class="text-sm font-medium text-gray-900 dark:text-white">
        {{ $t('promptParams.title', { title: prompt?.title || $t('promptParams.untitled') }) }}
      </div>
      <div v-if="prompt?.text" class="text-xs text-gray-500 dark:text-gray-400 mt-1 line-clamp-2">{{ prompt.text }}</div>

      <div class="mt-4 space-y-3 max-h-[60vh] overflow-auto pe-1">
        <div v-for="param in parameters" :key="param.name">
          <label class="block text-[11px] text-gray-500 dark:text-gray-400 mb-0.5">
            {{ param.label || param.name }}
            <span v-if="param.required" class="text-red-500">*</span>
          </label>

          <!-- enum -->
          <USelect
            v-if="param.type === 'enum'"
            v-model="values[param.name]"
            :options="param.options || []"
            size="sm"
            :placeholder="$t('promptParams.selectOption')"
          />

          <!-- date_range -->
          <div v-else-if="param.type === 'date_range'" class="flex items-center gap-2">
            <UInput v-model="rangeValues[param.name].start" type="date" size="sm" class="flex-1" />
            <span class="text-xs text-gray-400">{{ $t('promptParams.to') }}</span>
            <UInput v-model="rangeValues[param.name].end" type="date" size="sm" class="flex-1" />
          </div>

          <!-- number -->
          <UInput
            v-else-if="param.type === 'number'"
            v-model.number="values[param.name]"
            type="number"
            size="sm"
          />

          <!-- date -->
          <UInput
            v-else-if="param.type === 'date'"
            v-model="values[param.name]"
            type="date"
            size="sm"
          />

          <!-- text (default) -->
          <UInput
            v-else
            v-model="values[param.name]"
            type="text"
            size="sm"
          />
        </div>

        <div v-if="parameters.length === 0" class="text-xs text-gray-500 dark:text-gray-400">
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
import type { PromptParameter, PromptParamValue } from '~/composables/usePromptFill'

const props = defineProps<{
  modelValue: boolean
  prompt: any
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'confirm', values: Record<string, PromptParamValue>): void
  (e: 'cancel'): void
}>()

const isOpen = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const parameters = computed<PromptParameter[]>(() => (props.prompt?.parameters || []) as PromptParameter[])

// Scalar param values (text / number / date / enum), keyed by param name.
const values = ref<Record<string, any>>({})
// date_range values are { start, end } pairs, kept in a separate map so the
// two UInputs can bind independently.
const rangeValues = ref<Record<string, { start: string; end: string }>>({})

function seed() {
  const v: Record<string, any> = {}
  const rv: Record<string, { start: string; end: string }> = {}
  for (const p of parameters.value) {
    if (p.type === 'date_range') {
      const d: any = p.default || {}
      rv[p.name] = { start: d.start || '', end: d.end || '' }
    } else {
      v[p.name] = p.default ?? (p.type === 'number' ? undefined : '')
    }
  }
  values.value = v
  rangeValues.value = rv
}

// Re-seed whenever the modal opens for a (possibly different) prompt.
watch(() => [props.modelValue, props.prompt?.id], ([open]) => {
  if (open) seed()
}, { immediate: true })

function isFilled(p: PromptParameter): boolean {
  if (p.type === 'date_range') {
    const r = rangeValues.value[p.name] || { start: '', end: '' }
    return !!r.start && !!r.end
  }
  const val = values.value[p.name]
  if (p.type === 'number') return val !== undefined && val !== null && val !== ''
  return val !== undefined && val !== null && String(val).trim().length > 0
}

const canConfirm = computed(() => parameters.value.every(p => !p.required || isFilled(p)))

function collect(): Record<string, PromptParamValue> {
  const out: Record<string, PromptParamValue> = {}
  for (const p of parameters.value) {
    if (p.type === 'date_range') {
      out[p.name] = { ...(rangeValues.value[p.name] || { start: '', end: '' }) }
    } else {
      out[p.name] = values.value[p.name]
    }
  }
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
