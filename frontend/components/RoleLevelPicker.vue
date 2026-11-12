<template>
    <!-- Small segmented control used across the role editor: one choice per
         row (agent level, connection level, code visibility). A value that
         matches no option renders with nothing selected (the caller shows a
         "Custom" chip next to it). -->
    <div
        class="inline-flex shrink-0 rounded-md border border-line overflow-hidden bg-surface"
        role="radiogroup"
        :aria-label="label"
    >
        <button
            v-for="(opt, i) in options"
            :key="opt.value"
            type="button"
            role="radio"
            :aria-checked="modelValue === opt.value"
            :disabled="disabled"
            class="px-2.5 py-1 text-xs whitespace-nowrap transition-colors"
            :class="[
                i > 0 ? 'border-s border-line' : '',
                modelValue === opt.value
                    ? 'bg-blue-500 text-white font-medium'
                    : 'text-ink-muted hover:bg-surface-subtle',
                disabled ? 'opacity-50 cursor-not-allowed' : '',
            ]"
            @click="$emit('update:modelValue', opt.value)"
        >
            {{ opt.label }}
        </button>
    </div>
</template>

<script setup lang="ts">
defineProps<{
    modelValue: string | null
    options: { value: string; label: string }[]
    label?: string
    disabled?: boolean
}>()
defineEmits<{ (e: 'update:modelValue', value: string): void }>()
</script>
