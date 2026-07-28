<template>
  <div class="inline-flex items-center gap-1" role="group" :aria-label="label">
    <span v-if="label" class="text-[11px] text-gray-400 dark:text-gray-500 me-0.5">{{ label }}</span>
    <button
      v-for="o in options" :key="o.value"
      type="button"
      :aria-pressed="modelValue.includes(o.value)"
      :class="[
        'inline-flex items-center gap-1.5 h-8 px-2.5 rounded-lg border text-xs whitespace-nowrap transition-colors',
        modelValue.includes(o.value)
          ? 'border-transparent bg-gray-900 dark:bg-gray-200 text-white dark:text-gray-900 font-medium'
          : 'border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50'
      ]"
      @click="toggle(o.value)"
    >
      <span v-if="o.dot" class="w-[7px] h-[7px] rounded-full" :class="o.dot"></span>
      {{ o.label }}
      <span
        v-if="o.count !== undefined"
        class="font-mono tabular-nums"
        :class="modelValue.includes(o.value) ? '' : 'text-gray-900 dark:text-white font-semibold'"
      >{{ o.count }}</span>
    </button>
  </div>
</template>

<script setup lang="ts">
/**
 * A short, fixed set of options shown all at once.
 *
 * Used where a dropdown would be the wrong shape: four choices that never grow
 * cost one click here and two plus a scan behind a menu. Anything unbounded
 * (agents, people) belongs in MultiSelectFilter instead.
 */
const props = defineProps<{
  modelValue: string[]
  options: { value: string; label: string; dot?: string; count?: number }[]
  label?: string
}>()
const emit = defineEmits(['update:modelValue'])

const toggle = (v: string) => {
  const cur = [...(props.modelValue || [])]
  const i = cur.indexOf(v)
  if (i >= 0) cur.splice(i, 1)
  else cur.push(v)
  emit('update:modelValue', cur)
}
</script>
