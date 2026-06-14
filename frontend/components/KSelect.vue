<template>
  <UPopover :popper="{ placement: 'bottom-start' }" :ui="{ ring: '', shadow: 'shadow-md' }">
    <button type="button" class="inline-flex items-center gap-1.5 h-7 px-2 rounded-md text-[11px] text-gray-600 bg-gray-50 hover:bg-gray-100 transition-colors max-w-[180px]">
      <UIcon v-if="icon" :name="icon" class="w-3 h-3 text-gray-400 shrink-0" />
      <span class="truncate">{{ displayLabel }}</span>
      <UIcon name="i-heroicons-chevron-down" class="w-3 h-3 text-gray-300 shrink-0" />
    </button>
    <template #panel="{ close }">
      <div class="p-1 min-w-[170px] max-h-64 overflow-y-auto">
        <button
          v-for="opt in options" :key="opt.value"
          type="button"
          class="w-full flex items-center gap-2 px-2 py-1.5 text-[11px] rounded-md hover:bg-gray-50 text-left transition-colors"
          @click="pick(opt.value, close)"
        >
          <span v-if="multiple" class="w-3.5 h-3.5 rounded border flex items-center justify-center shrink-0" :class="isSel(opt.value) ? 'bg-gray-900 border-gray-900' : 'border-gray-300'">
            <UIcon v-if="isSel(opt.value)" name="i-heroicons-check" class="w-2.5 h-2.5 text-white" />
          </span>
          <UIcon v-else name="i-heroicons-check" class="w-3 h-3 shrink-0" :class="isSel(opt.value) ? 'text-gray-900' : 'text-transparent'" />
          <span class="text-gray-700 truncate">{{ opt.label }}</span>
        </button>
        <button
          v-if="multiple && (modelValue || []).length"
          type="button"
          class="w-full px-2 py-1.5 text-[11px] text-gray-500 hover:bg-gray-50 border-t border-gray-100 text-left mt-1"
          @click="$emit('update:modelValue', []); close && close()"
        >Clear</button>
      </div>
    </template>
  </UPopover>
</template>

<script setup lang="ts">
const props = defineProps<{
  modelValue: any
  options: { value: string; label: string }[]
  multiple?: boolean
  icon?: string
  placeholder?: string
}>()
const emit = defineEmits(['update:modelValue'])

const isSel = (v: string) => props.multiple ? (props.modelValue || []).includes(v) : props.modelValue === v
const pick = (v: string, close?: () => void) => {
  if (props.multiple) {
    const cur = [...(props.modelValue || [])]
    const i = cur.indexOf(v)
    i >= 0 ? cur.splice(i, 1) : cur.push(v)
    emit('update:modelValue', cur)
  } else {
    emit('update:modelValue', v)
    close && close()
  }
}
const displayLabel = computed(() => {
  if (props.multiple) {
    const a = props.modelValue || []
    if (!a.length) return props.placeholder || 'Any'
    return a.map((v: string) => props.options.find(o => o.value === v)?.label || v).join(', ')
  }
  return props.options.find(o => o.value === props.modelValue)?.label || props.placeholder || '—'
})
</script>
