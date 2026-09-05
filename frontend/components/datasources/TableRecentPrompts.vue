<template>
  <section class="mt-3 border-t border-gray-100 dark:border-gray-800 pt-3" data-testid="table-recent-prompts">
    <h3 class="text-xs font-medium text-gray-700 dark:text-gray-200">{{ t('tableErd.recentPrompts') }}</h3>
    <p class="text-[10px] text-gray-500 mt-1">{{ t('tableErd.thisAgentOnly') }}</p>
    <ol class="divide-y divide-gray-100 dark:divide-gray-800">
      <li v-for="item in items" :key="item.execution_id" class="py-2">
        <p class="text-xs text-gray-700 dark:text-gray-200 whitespace-pre-wrap break-words [overflow-wrap:anywhere] max-h-32 overflow-auto" dir="auto">{{ item.prompt || t('tableErd.promptUnavailable') }}</p>
        <div class="flex items-center gap-1 mt-1 text-[10px] text-gray-500">
          <UIcon :name="item.success ? 'i-heroicons-check-circle' : 'i-heroicons-x-circle'" :class="item.success ? 'text-green-500' : 'text-red-500'" class="w-3 h-3" />
          <span>{{ t(item.success ? 'tableErd.successful' : 'tableErd.failed') }}</span>
          <time class="ms-auto" :datetime="item.used_at">{{ new Date(item.used_at).toLocaleDateString(locale) }}</time>
        </div>
      </li>
    </ol>
    <p v-if="loading" class="text-[11px] text-gray-500 py-2" role="status">{{ t('tableErd.loadingPrompts') }}</p>
    <div v-else-if="error" role="alert" class="text-[11px] text-gray-500 py-2">{{ t('tableErd.promptsError') }} <button class="text-blue-600" @click="load">{{ t('tableErd.retry') }}</button></div>
    <p v-else-if="!items.length" class="text-[11px] text-gray-500 py-2">{{ t('tableErd.noPrompts') }}</p>
    <button v-if="!loading && !error && nextOffset !== null" class="text-[11px] text-blue-600 py-1" @click="load">{{ t('tableErd.morePrompts') }}</button>
  </section>
</template>
<script setup lang="ts">
const props = defineProps<{ agentId: string; tableId: string }>()
const { t, locale } = useI18n()
type Prompt = { execution_id: string; prompt: string; used_at: string; success: boolean }
const items = ref<Prompt[]>([])
const nextOffset = ref<number | null>(0)
const loading = ref(false)
const error = ref(false)
let generation = 0
async function load() {
  const requestGeneration = generation
  if (loading.value || nextOffset.value === null) return
  loading.value = true
  error.value = false
  const { data, error: fetchError } = await useMyFetch<{ items: Prompt[]; next_offset: number | null }>(
    `/data_sources/${encodeURIComponent(props.agentId)}/tables/${encodeURIComponent(props.tableId)}/recent-prompts`,
    { query: { offset: nextOffset.value, limit: 5 } },
  )
  if (requestGeneration !== generation) return
  loading.value = false
  if (fetchError.value || !data.value) { error.value = true; return }
  items.value.push(...data.value.items)
  nextOffset.value = data.value.next_offset
}
watch(() => [props.agentId, props.tableId], () => {
  generation++
  items.value = []
  nextOffset.value = 0
  loading.value = false
  load()
}, { immediate: true })
onBeforeUnmount(() => { generation++ })
</script>
