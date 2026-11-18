<template>
  <div class="min-w-0">
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-document-text" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status !== 'running' && routed">{{ isFallback ? t('tools.routeModel.fellBackTo') : t('tools.routeModel.routedTo') }}{{ ' ' }}{{ modelName }}</template>
      <template v-else>{{ label }}</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted">
      <template v-if="status !== 'running' && routed">
      <template v-if="isDecision && confidencePct !== null"><span class="align-middle ms-1 text-ink-muted cursor-help" data-testid="route-decision-confidence">
            · {{ t('tools.routeModel.decisionMatch', { pct: confidencePct }) }}</span></template><span v-if="isFallback && fromModel" class="align-middle ms-1 text-ink-muted" data-testid="fallback-switch-reason">
          — {{ t('tools.routeModel.fallbackReason', { from: fromModel }) }}</span></template>
      <template v-else></template></div>
  
    </div>
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)

import { computed } from 'vue'
import { useResponseI18n } from '~/composables/useResponseI18n'
import LLMProviderIcon from '~/components/LLMProviderIcon.vue'

const { t } = useResponseI18n()

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_summary?: string
  result_json?: any
}

const props = defineProps<{ toolExecution: ToolExecution }>()

const status = computed<string>(() => props.toolExecution?.status || '')
const rj = computed<any>(() => props.toolExecution?.result_json || {})

const routed = computed<boolean>(() => !!rj.value.routed)
// Availability fallback (harness-chosen) renders with its own wording; a
// planner-chosen escalation stays "Routed to".
const isFallback = computed<boolean>(() => rj.value.cause === 'fallback')
const isDecision = computed<boolean>(() => rj.value.cause === 'decision')
const confidencePct = computed<number | null>(() =>
  typeof rj.value.confidence === 'number' ? Math.round(rj.value.confidence * 100) : null)
const decisionTooltip = computed<string>(() => {
  const probs = Array.isArray(rj.value.probabilities) ? rj.value.probabilities.slice(0, 3) : []
  const parts = probs.map((p: any) => `${p.value} ${Math.round((p.probability || 0) * 100)}%`).join(' · ')
  const by = rj.value.decision_model ? t('tools.routeModel.decisionBy', { model: rj.value.decision_model }) : ''
  return [parts, by].filter(Boolean).join(' — ')
})
const fromModel = computed<string | null>(() => rj.value.from_model || null)
// Prefer the friendly model name; fall back to the provider model_id.
const modelName = computed<string>(() => rj.value.model_name || rj.value.model || 'model')
const providerType = computed<string | null>(() => rj.value.provider_type || null)

const label = computed<string>(() => {
  if (status.value === 'running') return t('tools.routeModel.choosing')
  // Not routed (kept current model, or invalid target) — fall back to the summary.
  return props.toolExecution?.result_summary || t('tools.routeModel.routing')
})
</script>
