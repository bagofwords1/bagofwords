<template>
  <div
    class="group relative flex flex-col h-full border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 rounded-lg p-4 hover:shadow-md hover:border-gray-300 dark:hover:border-gray-700 transition-all cursor-pointer"
    @click="$emit('view', prompt)"
  >
    <!-- Header: scope chip + manage actions (hover) -->
    <div class="flex items-start justify-between gap-2 mb-2">
      <!-- Agent scope: real connection-type icon(s) + agent name(s) -->
      <div v-if="prompt.scope === 'agent'" class="flex flex-wrap items-center gap-1">
        <span
          v-for="a in agentChips"
          :key="a.id"
          class="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded border text-violet-700 border-violet-200 bg-violet-50 dark:text-violet-300 dark:border-violet-900 dark:bg-violet-900/20"
        >
          <DataSourceIcon :type="a.type" :icon="a.icon" class="h-3 w-auto" />
          {{ a.name }}
        </span>
      </div>
      <!-- Global / Private: simple icon chip -->
      <span
        v-else
        class="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded border"
        :class="scopeChipClass"
      >
        <UIcon :name="scopeIcon" class="w-3 h-3" />
        {{ scopeLabel }}
      </span>

      <div
        v-if="prompt.can_manage"
        class="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity"
      >
        <button
          class="p-1 rounded text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800"
          :title="$t('prompts.edit')"
          @click.stop="$emit('edit', prompt)"
        >
          <UIcon name="heroicons-pencil-square" class="w-3.5 h-3.5" />
        </button>
        <button
          class="p-1 rounded text-gray-400 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20"
          :title="$t('prompts.delete')"
          @click.stop="$emit('delete', prompt)"
        >
          <UIcon name="heroicons-trash" class="w-3.5 h-3.5" />
        </button>
      </div>
    </div>

    <!-- Title -->
    <div class="text-sm font-medium text-gray-900 dark:text-white line-clamp-1 mb-1">
      {{ prompt.title || $t('prompts.untitled') }}
    </div>

    <!-- Text preview with {{param}} highlighted -->
    <div class="text-xs leading-relaxed text-gray-500 dark:text-gray-400 line-clamp-4 flex-1">
      <template v-for="(seg, i) in previewSegments" :key="i">
        <span
          v-if="seg.param"
          class="text-indigo-500 dark:text-indigo-400 font-medium bg-indigo-50 dark:bg-indigo-900/20 rounded px-0.5"
        >{{ seg.text }}</span>
        <span v-else>{{ seg.text }}</span>
      </template>
    </div>

    <!-- Meta chips -->
    <div class="flex flex-wrap items-center gap-1.5 mt-3">
      <span
        v-if="paramCount > 0"
        class="text-[10px] px-1.5 py-0.5 rounded border border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-800"
      >
        {{ $t('prompts.nParams', { n: paramCount }) }}
      </span>
      <span
        v-if="prompt.mode && prompt.mode !== 'chat'"
        class="text-[10px] px-1.5 py-0.5 rounded border border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-800"
      >
        {{ prompt.mode }}
      </span>
    </div>

    <!-- Footer: Run -->
    <div class="flex justify-end mt-3 pt-3 border-t border-gray-100 dark:border-gray-800">
      <button
        class="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium text-white bg-blue-500 hover:bg-blue-600 rounded-md transition-colors disabled:opacity-50"
        :disabled="running"
        @click.stop="$emit('run', prompt)"
      >
        <Spinner v-if="running" class="w-3 h-3 animate-spin" />
        <UIcon v-else name="heroicons-play" class="w-3.5 h-3.5" />
        {{ $t('prompts.run') }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import Spinner from '@/components/Spinner.vue'
import type { Prompt } from '~/composables/usePrompts'
import { usePromptFill } from '~/composables/usePromptFill'

const props = defineProps<{
  prompt: Prompt
  // id → { name, type, icon } for agent-scoped prompts (drives DataSourceIcon).
  agentMap?: Record<string, { name: string; type?: string; icon?: string | null }>
  running?: boolean
}>()

defineEmits<{
  (e: 'view', p: Prompt): void
  (e: 'edit', p: Prompt): void
  (e: 'delete', p: Prompt): void
  (e: 'run', p: Prompt): void
}>()

const { t } = useI18n()
const { extractParamNames } = usePromptFill()

// Parameters are bare {{name}} placeholders derived from the prompt text.
const paramCount = computed(() => extractParamNames(props.prompt.text || '').length)

// Agent chips: real connection-type icon + agent name, from the agent map.
const agentChips = computed(() =>
  (props.prompt.data_source_ids || []).map(id => ({
    id,
    name: props.agentMap?.[id]?.name || t('prompts.scopeAgent'),
    type: props.agentMap?.[id]?.type,
    icon: props.agentMap?.[id]?.icon,
  })),
)

const scopeIcon = computed(() => {
  if (props.prompt.scope === 'global') return 'heroicons-globe-alt'
  if (props.prompt.scope === 'agent') return 'heroicons-cube'
  return 'heroicons-lock-closed'
})

// Only used for the global/private chip (agent scope renders icon chips above).
const scopeLabel = computed(() => {
  if (props.prompt.scope === 'global') return t('prompts.scopeGlobal')
  return t('prompts.scopePrivate')
})

const scopeChipClass = computed(() => {
  if (props.prompt.scope === 'global') return 'text-blue-700 border-blue-200 bg-blue-50 dark:text-blue-300 dark:border-blue-900 dark:bg-blue-900/20'
  if (props.prompt.scope === 'agent') return 'text-violet-700 border-violet-200 bg-violet-50 dark:text-violet-300 dark:border-violet-900 dark:bg-violet-900/20'
  return 'text-gray-600 border-gray-200 bg-gray-50 dark:text-gray-300 dark:border-gray-700 dark:bg-gray-800'
})

// Split text into segments, marking {{param}} spans so the template can
// highlight them. Mirrors the placeholder regex used by usePromptFill.
const previewSegments = computed(() => {
  const text = props.prompt.text || ''
  const segments: { text: string; param: boolean }[] = []
  const re = /\{\{\s*[\p{L}\p{N}_.-]+(?:[ \t]+[\p{L}\p{N}_.-]+)*\s*\}\}/gu
  let last = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) segments.push({ text: text.slice(last, m.index), param: false })
    segments.push({ text: m[0], param: true })
    last = m.index + m[0].length
  }
  if (last < text.length) segments.push({ text: text.slice(last), param: false })
  return segments
})
</script>
