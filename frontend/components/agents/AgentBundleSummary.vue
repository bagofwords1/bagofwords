<template>
  <!-- What is inside an uploaded agent bundle (from POST /agents/import/preview). -->
  <div class="rounded-lg border border-line bg-surface-subtle p-3" data-testid="bundle-summary">
    <div class="flex items-start gap-3">
      <div class="w-8 h-8 rounded-md bg-white dark:bg-gray-900 border border-line flex items-center justify-center shrink-0">
        <UIcon name="i-heroicons-archive-box" class="w-4 h-4 text-ink-muted" />
      </div>
      <div class="min-w-0 flex-1">
        <div class="text-sm font-medium text-ink truncate">{{ preview.agent_name }}</div>
        <div v-if="fileName" class="text-2xs text-ink-muted truncate">{{ fileName }}</div>
        <p v-if="preview.description" class="text-xs text-ink-2 mt-1 line-clamp-2">{{ preview.description }}</p>
        <div class="flex flex-wrap gap-1.5 mt-2">
          <span v-for="chip in chips" :key="chip" class="inline-flex items-center px-1.5 py-0.5 rounded bg-white dark:bg-gray-900 border border-line text-2xs text-ink-2 nums">{{ chip }}</span>
        </div>
      </div>
    </div>
    <details v-if="preview.warnings.length" class="mt-2 text-2xs text-ink-muted">
      <summary class="cursor-pointer">{{ $t('agentImport.bundleWarnings', { n: preview.warnings.length }) }}</summary>
      <ul class="list-disc ps-4 mt-1 space-y-0.5"><li v-for="(w, i) in preview.warnings" :key="i">{{ w }}</li></ul>
    </details>
  </div>
</template>

<script setup lang="ts">
const props = defineProps<{ preview: any, fileName?: string | null }>()
const { t } = useI18n()

const chips = computed(() => {
  const c = props.preview.counts
  const tables = props.preview.connections.reduce((n: number, x: any) => n + x.tables_total, 0)
  return [
    t('agentImport.chipConnections', { n: props.preview.connections.length }),
    t('agentImport.chipTables', { n: tables }),
    t('agentImport.chipInstructions', { n: c.instructions }),
    c.evals ? t('agentImport.chipEvals', { n: c.evals }) : null,
    c.queries ? t('agentImport.chipQueries', { n: c.queries }) : null,
  ].filter(Boolean) as string[]
})
</script>
