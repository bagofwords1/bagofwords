<template>
  <div
    class="flex items-center gap-2 my-1.5 text-xs text-gray-500 dark:text-gray-400 min-w-0"
    :data-testid="`wave-strip-${wave.id}`"
    dir="auto"
  >
    <Spinner v-if="isLive" class="w-3 h-3 flex-shrink-0 text-gray-400" />
    <Icon v-else :name="wave.status === 'stopped' ? 'heroicons-stop-circle' : 'heroicons-squares-2x2'" class="w-3.5 h-3.5 flex-shrink-0 text-gray-400" />
    <span class="truncate">
      {{ t('subAgents.wave', { n: waveNumber }) }} · {{ t('subAgents.count', { count: wave.total || wave.sub_agents.length }) }}
      <template v-if="isLive"> · {{ t('subAgents.running', { count: runningCount }) }}</template>
      <template v-else-if="wave.status === 'stopped'"> · {{ t('subAgents.stoppedWave') }}</template>
      <template v-else-if="wave.status === 'timed_out'"> · {{ t('subAgents.timedOut') }}</template>
      <template v-else> · {{ outcomeSummary }}</template>
    </span>
    <button
      v-if="isLive && !readonly"
      class="ms-auto inline-flex items-center gap-0.5 text-[10px] text-red-600 hover:text-red-800 flex-shrink-0"
      :disabled="stopping"
      data-testid="wave-stop-all"
      @click.stop="emit('stopWave', wave.id)"
    >
      <Icon name="heroicons-stop" class="w-3 h-3" />
      <span>{{ stopping ? t('subAgents.stoppingAll') : t('subAgents.stopAll') }}</span>
    </button>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import Spinner from '~/components/Spinner.vue'

const { t } = useI18n()
const props = defineProps<{ wave: any; waveNumber: number; readonly?: boolean; stopping?: boolean }>()
const emit = defineEmits<{ (e: 'stopWave', waveId: string): void }>()

const isLive = computed(() => ['collecting', 'running', 'firing'].includes(props.wave?.status))
const runningCount = computed(() => (props.wave?.sub_agents || []).filter((r: any) => r.status === 'running' || r.status === 'pending').length)
const outcomeSummary = computed(() => {
  const counts: Record<string, number> = {}
  for (const r of props.wave?.sub_agents || []) {
    const k = r.outcome || r.status
    counts[k] = (counts[k] || 0) + 1
  }
  return Object.entries(counts).map(([k, n]) => `${n} ${t(`tools.delegate.outcomes.${k}`, k)}`).join(' · ')
})
</script>
