<template>
  <UTooltip :ui="{ base: 'h-auto px-3 py-2 text-xs font-normal whitespace-normal overflow-visible relative', width: 'w-56' }" :popper="{ placement: 'top', strategy: 'fixed' }">
    <button type="button" data-testid="erd-metrics" class="nodrag nopan text-[10px] text-gray-500 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200" @click.stop>
      {{ t('tableErd.usageValue', { count: number(table.usage_count) }) }}
      <span v-if="table.last_refresh_status === 'error'" class="ms-1 text-red-500">· {{ t('tableErd.refreshFailed') }}</span>
    </button>
    <template #text>
      <dl data-testid="table-metrics-details" class="space-y-1.5">
        <div v-for="item in metrics" :key="item.label" class="flex justify-between gap-4"><dt class="text-gray-500 dark:text-gray-400">{{ item.label }}</dt><dd>{{ item.value }}</dd></div>
      </dl>
    </template>
  </UTooltip>
</template>
<script setup lang="ts">
import type { GraphTable } from '~/utils/tableGraph'
const props = defineProps<{ table: GraphTable }>()
const { t, locale } = useI18n()
const number = (value?: number) => value == null ? '—' : new Intl.NumberFormat(locale.value).format(value)
const date = (value?: string) => value ? new Intl.DateTimeFormat(locale.value, { dateStyle: 'medium' }).format(new Date(value)) : '—'
const metrics = computed(() => [
  { label: t('tableErd.usage'), value: number(props.table.usage_count) },
  { label: t('tableErd.successful'), value: number(props.table.success_count) },
  { label: t('tableErd.failed'), value: number(props.table.failure_count) },
  { label: t('tableErd.feedback'), value: props.table.pos_feedback_count == null && props.table.neg_feedback_count == null ? '—' : `+${number(props.table.pos_feedback_count)} / −${number(props.table.neg_feedback_count)}` },
  { label: t('tableErd.lastUsed'), value: date(props.table.last_used_at) },
  ...(props.table.custom_query_id ? [
    { label: t('tableErd.lastRefreshed'), value: date(props.table.last_refreshed_at) },
    { label: t('tableErd.cache'), value: t(props.table.last_refresh_status === 'error' ? 'tableErd.refreshFailed' : props.table.last_refreshed_at ? 'tableErd.cached' : 'tableErd.notCached') },
  ] : []),
])
</script>
