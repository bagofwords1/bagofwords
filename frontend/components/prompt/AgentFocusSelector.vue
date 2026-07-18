<template>
  <UPopover :popper="popper">
    <UTooltip :text="tooltip" :popper="{ strategy: 'fixed', placement: 'top' }">
      <button
        type="button"
        class="text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white hover:bg-gray-50 dark:hover:bg-gray-800/50 rounded-md px-2 py-1 text-xs flex items-center max-w-[200px] border border-gray-200 dark:border-gray-700"
      >
        <Icon name="heroicons-view-columns" class="w-4 h-4 flex-shrink-0" />
        <span class="ms-1 truncate">{{ label }}</span>
      </button>
    </UTooltip>
    <template #panel="{ close }">
      <div class="p-2 text-xs max-h-72 overflow-y-auto w-[240px]">
        <div class="px-2 pb-1 text-[10px] uppercase tracking-wide text-gray-400">Focus agents</div>
        <!-- Auto (no explicit focus) -->
        <div
          class="px-2 py-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800/70 cursor-pointer flex items-center"
          @click="() => { setAuto(); close(); }"
        >
          <div class="me-2"><Icon name="heroicons-sparkles" class="w-4 h-4 text-gray-400" /></div>
          <div class="flex flex-col flex-1 text-start min-w-0">
            <span>Auto</span>
            <span class="text-gray-500 dark:text-gray-400 text-[10px] truncate">All agents; the AI picks per question</span>
          </div>
          <Icon v-if="!hasFocus" name="heroicons-check" class="w-4 h-4 text-blue-500 ms-2 flex-shrink-0" />
        </div>
        <div class="my-1 border-t border-gray-100 dark:border-gray-800" />
        <!-- Attached agents (multi-select) -->
        <div
          v-for="a in agents"
          :key="a.id"
          class="px-2 py-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800/70 cursor-pointer flex items-center"
          @click="toggle(a.id)"
        >
          <div class="me-2"><Icon name="heroicons-circle-stack" class="w-4 h-4 text-gray-400" /></div>
          <div class="flex flex-col flex-1 text-start min-w-0">
            <span class="font-medium truncate" :title="a.name">{{ a.name }}</span>
          </div>
          <Icon v-if="isFocused(a.id)" name="heroicons-check" class="w-4 h-4 text-blue-500 ms-2 flex-shrink-0" />
        </div>
      </div>
    </template>
  </UPopover>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'

// Mirrors the LLM ModelSelector: a compact prompt-box chip. "Focus" is the
// subset of attached agents whose full schema is loaded into the AI's context.
// Auto (no focus) lets the AI pick per question from the thin agent roster.
const props = defineProps<{
  agents: any[]            // attached data sources (agents) for this report
  reportId?: string | null
  focusedIds?: string[] | null
}>()

const emit = defineEmits<{
  (e: 'update:focusedIds', v: string[]): void
}>()

const popper = { strategy: 'absolute' as const, placement: 'bottom-start' as const, offset: [0, 8] }

const internal = ref<string[]>([...(props.focusedIds || [])])
watch(() => props.focusedIds, (v) => { internal.value = [...(v || [])] })

const hasFocus = computed(() => internal.value.length > 0)
const isFocused = (id: string) => internal.value.includes(id)

const label = computed(() => {
  if (!hasFocus.value) return 'Auto'
  if (internal.value.length === 1) {
    const a = props.agents.find(x => x.id === internal.value[0])
    return a?.name || '1 agent'
  }
  return `${internal.value.length} agents`
})
const tooltip = computed(() =>
  hasFocus.value ? `Focused on ${internal.value.length} agent(s)` : 'Auto — AI picks agents from the roster')

async function persist() {
  emit('update:focusedIds', [...internal.value])
  if (!props.reportId) return
  try {
    await useMyFetch(`/reports/${props.reportId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ focused_data_source_ids: internal.value }),
    })
    window.dispatchEvent(new CustomEvent('report:mutated', { detail: { reportId: props.reportId, kind: 'agent_focus' } }))
  } catch {}
}

function toggle(id: string) {
  if (internal.value.includes(id)) internal.value = internal.value.filter(x => x !== id)
  else internal.value = [...internal.value, id]
  persist()
}
function setAuto() {
  internal.value = []
  persist()
}
</script>
