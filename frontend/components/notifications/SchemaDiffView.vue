<template>
  <div class="flex flex-col gap-2 text-xs" data-testid="schema-diff">
    <div v-if="diff.added_tables?.length" class="flex flex-col gap-0.5">
      <span class="text-2xs font-semibold uppercase tracking-wide text-ink-muted">{{ $t('notifications.diff.addedTables') }}</span>
      <div class="flex flex-wrap gap-1">
        <code v-for="tb in diff.added_tables" :key="'a' + tb" dir="ltr" class="px-1.5 py-px rounded bg-green-50 text-green-700 dark:bg-green-500/10 dark:text-green-300">+ {{ tb }}</code>
      </div>
    </div>
    <div v-if="diff.removed_tables?.length" class="flex flex-col gap-0.5">
      <span class="text-2xs font-semibold uppercase tracking-wide text-ink-muted">{{ $t('notifications.diff.removedTables') }}</span>
      <div class="flex flex-wrap gap-1">
        <code v-for="tb in diff.removed_tables" :key="'r' + tb" dir="ltr" class="px-1.5 py-px rounded bg-red-50 text-red-700 dark:bg-red-500/10 dark:text-red-300">− {{ tb }}</code>
      </div>
    </div>
    <div v-if="diff.changed?.length" class="flex flex-col gap-1">
      <span class="text-2xs font-semibold uppercase tracking-wide text-ink-muted">{{ $t('notifications.diff.changedTables') }}</span>
      <div v-for="ch in diff.changed" :key="'c' + ch.table" class="flex flex-col gap-0.5 rounded-md border border-gray-100 dark:border-gray-800 px-2 py-1.5">
        <code dir="ltr" class="self-start font-semibold text-gray-800 dark:text-gray-200">{{ ch.table }}</code>
        <div class="flex flex-wrap gap-1">
          <code v-for="c in ch.added_cols" :key="'ac' + c.name" dir="ltr" class="px-1.5 py-px rounded bg-green-50 text-green-700 dark:bg-green-500/10 dark:text-green-300">+ {{ c.name }}<span v-if="c.type" class="opacity-70 ms-1">{{ c.type }}</span></code>
          <code v-for="c in ch.removed_cols" :key="'rc' + c" dir="ltr" class="px-1.5 py-px rounded bg-red-50 text-red-700 dark:bg-red-500/10 dark:text-red-300">− {{ c }}</code>
          <code v-for="c in ch.type_changes" :key="'tc' + c.col" dir="ltr" class="px-1.5 py-px rounded bg-amber-50 text-amber-700 dark:bg-amber-500/10 dark:text-amber-300">{{ c.col }}: {{ c.from || '?' }} → {{ c.to || '?' }}</code>
        </div>
      </div>
    </div>
    <p v-if="more > 0" class="text-2xs text-ink-muted">{{ $t('notifications.diff.more', { n: more }) }}</p>
    <div v-if="diff.new_inactive_tables?.length" class="flex flex-col gap-0.5">
      <span class="text-2xs text-ink-muted">{{ $t('notifications.diff.newInactive') }}</span>
      <div class="flex flex-wrap gap-1">
        <code v-for="tb in diff.new_inactive_tables" :key="'n' + tb" dir="ltr" class="px-1.5 py-px rounded bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400">{{ tb }}</code>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// Renders the structured schema diff a schema-change notification carries
// (backend app/services/schema_diff.py). Table and column names are code, so
// they stay LTR inside an RTL layout.
const props = defineProps<{ diff: Record<string, any> }>()

// Tables beyond the per-list cap: total counts every change, the lists are cut.
const more = computed(() => {
  const d = props.diff || {}
  const shown = (d.added_tables?.length || 0) + (d.removed_tables?.length || 0) + (d.changed?.length || 0)
  return Math.max(0, (d.total || 0) - shown)
})
</script>
