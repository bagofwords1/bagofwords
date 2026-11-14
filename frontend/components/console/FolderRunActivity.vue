<template>
  <section v-if="rows.length" class="my-4 rounded-lg border border-line bg-surface p-4">
    <h3 class="mb-2 text-sm font-medium text-ink">{{ $t('sharedFolders.activity') }}</h3>
    <div v-for="row in rows" :key="row.execution_id + row.agent_id"
         class="flex items-center gap-3 border-t border-line py-2 text-xs text-ink-muted">
      <span class="flex-1 text-ink">{{ row.agent_name }}</span>
      <time :datetime="row.created_at">{{ new Date(row.created_at).toLocaleString(locale) }}</time>
      <button v-if="row.report_id" class="text-accent" @click="selected = row">{{ $t('sharedFolders.viewTrace') }}</button>
    </div>
    <TraceModal :model-value="Boolean(selected)" :report-id="selected?.report_id || ''"
                :completion-id="selected?.completion_id || undefined" @update:model-value="selected = null" />
  </section>
</template>

<script setup lang="ts">
import TraceModal from './TraceModal.vue'
type Activity = { execution_id: string, agent_id: string, agent_name: string, created_at: string, report_id: string | null, completion_id: string | null }
const { locale } = useI18n()
const rows = ref<Activity[]>([])
const selected = ref<Activity | null>(null)
const props = defineProps<{ start?: string, end?: string }>()
let generation = 0
async function refresh() {
  const request = ++generation
  const { data } = await useMyFetch<Activity[]>('/api/console/folder-activity', {query: {start: props.start, end: props.end}})
  if (request === generation) rows.value = data.value || []
}
onMounted(refresh)
watch(() => [props.start, props.end], refresh)
</script>
