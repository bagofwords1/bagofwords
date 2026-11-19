<template>
  <div class="tool-call-heading flex min-w-0 items-center gap-2">
    <button type="button"
      class="tool-call-control my-0.5 inline-flex w-fit max-w-full items-center gap-1.5 rounded py-0.5 text-start text-chat text-ink-muted focus:outline-none focus-visible:ring-1 focus-visible:ring-blue-500"
      :aria-expanded="expandable ? expanded : undefined" :disabled="!expandable" @click="$emit('toggle')">
      <Icon :name="failed ? 'heroicons-exclamation-triangle' : icon" class="h-3.5 w-3.5 shrink-0" :class="{ 'text-amber-600': failed }" aria-hidden="true" />
      <span v-if="failed" class="sr-only">{{ t('tools.common.failed') }}</span>
      <bdi class="tool-call-label" :class="{ 'is-running': active }"><slot name="label">{{ label }}</slot></bdi>
      <Icon :name="expanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="h-3 w-3 shrink-0 rtl-flip" :class="{ invisible: !expandable }" aria-hidden="true" />
    </button>
    <slot name="actions" />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useResponseI18n } from '~/composables/useResponseI18n'
const { t } = useResponseI18n()
const props = withDefaults(defineProps<{
  toolExecution?: { status?: string; error_message?: unknown; result_json?: any }
  label?: string
  icon?: string
  expanded?: boolean
  expandable?: boolean
  running?: boolean
  error?: boolean
}>(), { icon: 'heroicons-wrench-screwdriver', expanded: false, expandable: true, running: undefined, error: undefined })
defineEmits<{ (e: 'toggle'): void }>()
const active = computed(() => props.running ?? ['running', 'in_progress', 'pending', 'queued'].includes(props.toolExecution?.status || ''))
const failed = computed(() => props.error ?? (!active.value && (
  ['error', 'failed'].includes(props.toolExecution?.status || '') ||
  !!props.toolExecution?.error_message || !!props.toolExecution?.result_json?.error ||
  !!props.toolExecution?.result_json?.error_message || props.toolExecution?.result_json?.success === false
)))
</script>

<style scoped>
.tool-call-control { color: rgb(var(--bow-ink-muted-rgb)); opacity: 1; }
.tool-call-label { min-width: 0; overflow-wrap: anywhere; color: inherit; -webkit-text-fill-color: currentColor; }
@supports ((background-clip: text) or (-webkit-background-clip: text)) {
  .tool-call-control:not(:hover):not(:focus-visible) .is-running {
    background-color: rgb(var(--bow-ink-muted-rgb));
    background-image: linear-gradient(100deg,
      rgb(var(--bow-ink-subtle-rgb)) 20%,
      rgb(var(--bow-ink-muted-rgb)) 32%,
      rgb(var(--bow-chat-ink-rgb)) 44%,
      rgb(var(--bow-chat-ink-rgb)) 56%,
      rgb(var(--bow-ink-muted-rgb)) 68%,
      rgb(var(--bow-ink-subtle-rgb)) 80%);
    background-size: 200% 100%;
    background-clip: text;
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    animation: bow-shimmer 2.2s linear infinite;
  }
}
.tool-call-control:hover, .tool-call-control:focus-visible { color: rgb(var(--bow-chat-ink-rgb)); }
.tool-call-control:hover .tool-call-label, .tool-call-control:focus-visible .tool-call-label {
  color: inherit;
  -webkit-text-fill-color: currentColor;
  background: none;
  animation: none;
}
@media (prefers-reduced-motion: reduce) {
  .tool-call-control:not(:hover):not(:focus-visible) .is-running {
    background: none;
    -webkit-text-fill-color: currentColor;
    animation: none;
  }
}
</style>
