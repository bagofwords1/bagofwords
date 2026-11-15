<template>
  <div class="mt-1">
    <div class="mb-2 flex items-center text-chat-2 text-gray-700 dark:text-gray-300">
      <Icon
        :name="status === 'running' ? 'heroicons-arrow-path' : 'heroicons-arrows-right-left'"
        class="w-3 h-3 me-1 text-ink-muted flex-shrink-0"
        :class="{ 'animate-spin': status === 'running' }"
      />
      <template v-if="status !== 'running' && routed">
        <span class="align-middle me-1">{{ isFallback ? t('tools.routeModel.fellBackTo') : t('tools.routeModel.routedTo') }}</span>
        <LLMProviderIcon v-if="providerType || modelName" :provider="providerType || 'custom'" :model="modelName" :icon="true" class="w-3.5 h-3.5 me-1 flex-shrink-0" />
        <span class="font-medium align-middle">{{ modelName }}</span>
        <!-- Upfront pick by a decision model: confidence, with the top
             probabilities and which decision model chose on hover. -->
        <UTooltip v-if="isDecision && confidencePct !== null" :text="decisionTooltip" :popper="{ placement: 'top' }">
          <span class="align-middle ms-1 text-ink-muted cursor-help" data-testid="route-decision-confidence">
            · {{ t('tools.routeModel.decisionMatch', { pct: confidencePct }) }}
          </span>
        </UTooltip>
        <span v-if="isFallback && fromModel" class="align-middle ms-1 text-ink-muted" data-testid="fallback-switch-reason">
          — {{ t('tools.routeModel.fallbackReason', { from: fromModel }) }}
        </span>
      </template>
      <span v-else class="align-middle">{{ label }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
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
