<template>
  <div class="flex flex-col h-full min-h-0" data-testid="agent-instructions-panel">
    <div class="shrink-0 px-3 pt-3 pb-2 flex items-center gap-2">
      <div class="relative flex-1">
        <UIcon name="i-heroicons-magnifying-glass" class="absolute start-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-ink-muted" />
        <input ref="panelSearchInput" :value="search || ''" type="text" :placeholder="$t('agentsPage.searchAgentInstructions')" data-testid="agent-instructions-search" class="w-full h-8 ps-8 text-sm bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 dark:text-gray-100 rounded-md outline-none focus:border-gray-400 focus:bg-white dark:focus:bg-gray-800 placeholder:text-ink-subtle dark:placeholder:text-gray-500" :class="search ? 'pe-8' : 'pe-2'" @input="emit('search', ($event.target as HTMLInputElement).value)" @keydown.escape="emit('search', '')" />
        <button v-if="search" type="button" class="absolute end-1.5 top-1/2 -translate-y-1/2 h-6 w-6 flex items-center justify-center rounded text-ink-muted hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700" :title="$t('agentsPage.clearSearch')" :aria-label="$t('agentsPage.clearSearch')" @click="emit('search', '')">
          <UIcon name="i-heroicons-x-mark" class="w-3.5 h-3.5" />
        </button>
      </div>
      <slot name="actions" />
      <span v-if="!loading" class="text-xs tabular-nums text-ink-muted shrink-0" data-testid="agent-instructions-count">{{ count }}</span>
      <button v-if="canAdd" type="button" class="h-8 px-2.5 rounded-md border border-gray-200 dark:border-gray-800 text-gray-600 dark:text-gray-400 text-xs font-medium hover:bg-gray-50 dark:hover:bg-gray-800/50 inline-flex items-center gap-1 shrink-0" @click="emit('add')"><UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" />{{ $t('agentsPage.addShort') }}</button>
    </div>
    <div class="flex-1 min-h-0 overflow-y-auto px-2 pb-3 space-y-0.5">
      <div v-if="loading" class="flex items-center gap-2 h-8 text-sm text-ink-muted" style="padding-inline-start:20px"><Spinner class="w-3.5 h-3.5" /><span>{{ $t('agentsPage.loading') }}</span></div>
      <slot v-else />
    </div>
  </div>
</template>
<script setup lang="ts">
defineProps<{search: string, loading: boolean, count: number, canAdd: boolean}>()
const emit = defineEmits<{search:[value:string], add:[]}>()
const panelSearchInput = ref<HTMLInputElement>()
defineExpose({focus: () => panelSearchInput.value?.focus()})
</script>
