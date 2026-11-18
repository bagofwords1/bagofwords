<template>
  <!-- What a PII rule does when its action has no replacement token to edit. -->
  <span :class="['text-xs flex items-center gap-1', action === 'block' ? 'text-red-600 dark:text-red-400' : 'text-ink-muted']">
    <Icon :name="icon" class="w-3.5 h-3.5 shrink-0" />
    {{ label }}
  </span>
</template>

<script setup lang="ts">
const props = defineProps<{ action: string }>()
const { t } = useI18n()

const icon = computed(() => ({
  block: 'heroicons:no-symbol',
  mask: 'heroicons:ellipsis-horizontal',
  pseudonymize: 'heroicons:arrows-right-left',
} as Record<string, string>)[props.action] || 'heroicons:pencil')

const label = computed(() => ({
  block: t('settings.pii.blocksRequest'),
  mask: t('settings.pii.masksValue'),
  pseudonymize: t('settings.pii.pseudonymizesValue'),
} as Record<string, string>)[props.action] || '')
</script>
