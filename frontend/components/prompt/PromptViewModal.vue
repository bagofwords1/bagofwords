<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-2xl' }">
    <UCard :ui="{ body: { padding: 'px-5 py-4 sm:p-5' }, header: { padding: 'px-5 py-3 sm:px-5 sm:py-3' }, footer: { padding: 'px-5 py-3 sm:px-5 sm:py-3' } }">
      <template #header>
        <div class="flex items-start justify-between gap-2">
          <div class="min-w-0">
            <div class="flex items-center gap-2">
              <span
                class="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded border"
                :class="scopeChipClass"
              >
                <UIcon :name="scopeIcon" class="w-3 h-3" />
                {{ scopeLabel }}
              </span>
            </div>
            <h3 class="mt-1.5 text-sm font-semibold text-gray-900 dark:text-white">
              {{ prompt?.title || $t('prompts.untitled') }}
            </h3>
          </div>
          <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" size="xs" @click="isOpen = false" />
        </div>
      </template>

      <div class="space-y-4">
        <!-- Full text -->
        <div>
          <div class="text-[11px] uppercase tracking-wide text-gray-400 dark:text-gray-500 mb-1">{{ $t('prompts.viewText') }}</div>
          <div class="text-sm leading-relaxed text-gray-700 dark:text-gray-200 whitespace-pre-wrap rounded-md border border-gray-100 dark:border-gray-800 bg-gray-50/60 dark:bg-gray-800/40 p-3">
            <template v-for="(seg, i) in textSegments" :key="i">
              <span
                v-if="seg.param"
                class="text-indigo-500 dark:text-indigo-400 font-medium bg-indigo-50 dark:bg-indigo-900/20 rounded px-0.5"
              >{{ seg.text }}</span>
              <span v-else>{{ seg.text }}</span>
            </template>
          </div>
        </div>

        <!-- Parameters -->
        <div v-if="paramNames.length">
          <div class="text-[11px] uppercase tracking-wide text-gray-400 dark:text-gray-500 mb-1">{{ $t('prompts.viewParameters') }}</div>
          <div class="flex flex-wrap gap-1.5">
            <code
              v-for="n in paramNames"
              :key="n"
              class="text-xs text-indigo-500 dark:text-indigo-400 bg-indigo-50 dark:bg-indigo-900/20 rounded px-1.5 py-0.5"
            >{{ placeholder(n) }}</code>
          </div>
        </div>

        <!-- Agents / scope: always explicit DataSourceIcon + name -->
        <div v-if="prompt?.scope === 'agent' && agentChips.length">
          <div class="text-[11px] uppercase tracking-wide text-gray-400 dark:text-gray-500 mb-1">{{ $t('prompts.viewAgents') }}</div>
          <div class="flex flex-wrap gap-1.5">
            <span
              v-for="a in agentChips"
              :key="a.id"
              class="inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded border border-violet-200 dark:border-violet-900 text-violet-700 dark:text-violet-300 bg-violet-50 dark:bg-violet-900/20"
            >
              <DataSourceIcon :type="a.type" :icon="a.icon" class="h-3 w-auto" />
              {{ a.name }}
            </span>
          </div>
        </div>

        <!-- Author / meta -->
        <div class="flex items-center gap-3 text-[11px] text-gray-400 dark:text-gray-500">
          <span v-if="authorName">{{ $t('prompts.byAuthor', { name: authorName }) }}</span>
          <span v-if="prompt?.created_at">· {{ formattedDate }}</span>
        </div>
      </div>

      <template #footer>
        <div class="flex items-center justify-between gap-2">
          <div class="flex items-center gap-2">
            <UButton
              v-if="prompt?.can_manage"
              color="red"
              variant="ghost"
              size="xs"
              icon="i-heroicons-trash"
              @click="$emit('delete', prompt)"
            >{{ $t('prompts.delete') }}</UButton>
          </div>
          <div class="flex items-center justify-end gap-2">
            <UButton
              v-if="prompt?.can_manage"
              color="gray"
              variant="soft"
              size="xs"
              icon="i-heroicons-user-group"
              @click="$emit('run-for', prompt)"
            >{{ $t('prompts.runFor') }}</UButton>
            <UButton
              v-if="prompt?.can_manage"
              color="gray"
              variant="soft"
              size="xs"
              icon="i-heroicons-pencil-square"
              @click="$emit('edit', prompt)"
            >{{ $t('prompts.edit') }}</UButton>
            <UButton
              color="blue"
              size="xs"
              icon="i-heroicons-play"
              :loading="running"
              @click="$emit('run', prompt)"
            >{{ $t('prompts.run') }}</UButton>
          </div>
        </div>
      </template>
    </UCard>
  </UModal>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { usePromptFill } from '~/composables/usePromptFill'
import type { Prompt } from '~/composables/usePrompts'

const { extractParamNames } = usePromptFill()

const props = defineProps<{
  modelValue: boolean
  prompt: Prompt | null
  // id → { name, type } for the agent chips' DataSourceIcon.
  agentMap?: Record<string, { name: string; type?: string; icon?: string | null }>
  authorNames?: Record<string, string>
  running?: boolean
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'run', p: Prompt | null): void
  (e: 'edit', p: Prompt | null): void
  (e: 'delete', p: Prompt | null): void
  (e: 'run-for', p: Prompt | null): void
}>()

const { t, d } = useI18n()

const isOpen = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

// Parameters are bare {{name}} placeholders derived from the prompt text.
const paramNames = computed<string[]>(() => extractParamNames(props.prompt?.text || ''))

// Render a `{{name}}` placeholder string without embedding the literal braces
// in the Vue template (which the SFC compiler would parse as interpolation).
function placeholder(name: string): string {
  return '{{' + name + '}}'
}

const agentChips = computed(() => {
  const ids = props.prompt?.data_source_ids || []
  // Fall back to the generic "Agent" label (matching PromptCard) when an agent
  // id can't be resolved to a name — never leak a bare UUID into the UI.
  return ids.map(id => ({ id, name: props.agentMap?.[id]?.name || t('prompts.scopeAgent'), type: props.agentMap?.[id]?.type, icon: props.agentMap?.[id]?.icon }))
})

const authorName = computed(() => {
  const uid = props.prompt?.user_id
  if (!uid) return ''
  return props.authorNames?.[uid] || ''
})

const formattedDate = computed(() => {
  const c = props.prompt?.created_at
  if (!c) return ''
  try { return d(new Date(c), 'short') } catch { return '' }
})

const scopeIcon = computed(() => {
  if (props.prompt?.scope === 'global') return 'heroicons-globe-alt'
  if (props.prompt?.scope === 'agent') return 'heroicons-cube'
  return 'heroicons-lock-closed'
})

const scopeLabel = computed(() => {
  if (props.prompt?.scope === 'global') return t('prompts.scopeGlobal')
  if (props.prompt?.scope === 'agent') return t('prompts.scopeAgent')
  return t('prompts.scopePrivate')
})

const scopeChipClass = computed(() => {
  if (props.prompt?.scope === 'global') return 'text-blue-700 border-blue-200 bg-blue-50 dark:text-blue-300 dark:border-blue-900 dark:bg-blue-900/20'
  if (props.prompt?.scope === 'agent') return 'text-violet-700 border-violet-200 bg-violet-50 dark:text-violet-300 dark:border-violet-900 dark:bg-violet-900/20'
  return 'text-gray-600 border-gray-200 bg-gray-50 dark:text-gray-300 dark:border-gray-700 dark:bg-gray-800'
})

const textSegments = computed(() => {
  const text = props.prompt?.text || ''
  const segments: { text: string; param: boolean }[] = []
  const re = /\{\{\s*[\w.-]+\s*\}\}/g
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
