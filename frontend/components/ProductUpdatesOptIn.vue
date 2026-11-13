<template>
  <div class="flex items-start gap-2.5" data-testid="product-updates-opt-in">
    <UCheckbox
      :id="inputId"
      :model-value="modelValue"
      class="mt-0.5"
      @update:model-value="emit('update:modelValue', !!$event)"
    />
    <label :for="inputId" class="text-sm leading-snug cursor-pointer select-none">
      <span class="text-gray-900 dark:text-gray-100">
        {{ email ? $t('productUpdates.checkboxWithEmail', { email }) : $t('productUpdates.checkbox') }}
      </span>
      <span class="block mt-0.5 text-xs text-ink-muted">{{ $t('productUpdates.disclosure') }}</span>
    </label>
  </div>
</template>

<script setup lang="ts">
// The instance admin's opt-in to release notes and product updates.
// Unticked by default; nothing is sent unless the admin ticks it.
withDefaults(defineProps<{ modelValue: boolean; email?: string | null; inputId?: string }>(), {
  email: null,
  inputId: 'product-updates',
})
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>()
</script>
