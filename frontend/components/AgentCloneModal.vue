<!--
  Clone agent: name + description for the new agent, and for each part of the
  source a choice of what the new agent gets — its own copy, the same (shared)
  rows, or nothing. One line under each row says what the current choice does.
  Settings, tables, tool policies and prompts always come along (server side).
-->
<template>
  <UModal :model-value="modelValue" :ui="{ width: 'sm:max-w-lg' }" @update:model-value="v => emit('update:modelValue', v)">
    <form class="p-5" data-testid="agent-clone-modal" @submit.prevent="submit">
      <div class="flex items-center gap-2">
        <UIcon name="i-heroicons-document-duplicate" class="w-4 h-4 text-gray-400 dark:text-gray-500" />
        <h2 dir="auto" class="text-sm font-semibold text-gray-900 dark:text-white truncate">{{ $t('agentClone.titleFor', { name: agent?.name }) }}</h2>
      </div>

      <label class="block mt-4 text-xs font-medium text-gray-700 dark:text-gray-300" for="agent-clone-name">{{ $t('agentClone.name') }}</label>
      <input
        id="agent-clone-name"
        ref="nameInput"
        v-model="name"
        dir="auto"
        type="text"
        maxlength="255"
        data-testid="agent-clone-name"
        class="mt-1 w-full h-9 text-sm border dark:bg-gray-800 dark:text-gray-100 rounded-md px-2.5 focus:outline-none focus:ring-2 focus:ring-blue-200 dark:focus:ring-blue-500/40"
        :class="nameError ? 'border-red-400' : 'border-gray-200 dark:border-gray-700'"
      />
      <p v-if="nameError" class="mt-1 text-[11px] text-red-600 dark:text-red-400">{{ nameError }}</p>

      <label class="block mt-3 text-xs font-medium text-gray-700 dark:text-gray-300" for="agent-clone-description">{{ $t('agentClone.description') }}</label>
      <textarea
        id="agent-clone-description"
        v-model="description"
        dir="auto"
        rows="2"
        class="mt-1 w-full text-sm border border-gray-200 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 rounded-md px-2.5 py-1.5 focus:outline-none focus:ring-2 focus:ring-blue-200 dark:focus:ring-blue-500/40"
      />

      <!-- What always comes along, so the rows below are only the real choices. -->
      <div class="mt-3 flex flex-wrap items-center gap-1.5 text-[11px] text-gray-500 dark:text-gray-400" data-testid="agent-clone-always">
        <span>{{ $t('agentClone.alwaysIncluded') }}</span>
        <span v-for="k in ALWAYS" :key="k" class="inline-flex items-center gap-1 rounded-full border border-gray-200 dark:border-gray-700 px-2 py-px">
          <UIcon name="i-heroicons-check" class="w-3 h-3 text-emerald-600 dark:text-emerald-400" />{{ $t(`agentClone.always.${k}`) }}
        </span>
      </div>

      <div class="mt-3 border-t border-gray-100 dark:border-gray-800">
        <div v-for="row in rows" :key="row.key" class="flex items-center gap-2.5 py-2.5 border-b border-gray-100 dark:border-gray-800" :data-testid="`agent-clone-row-${row.key}`">
          <LibraryIcon v-if="row.key === 'saved_queries'" class="w-4 h-4 shrink-0 text-gray-400 dark:text-gray-500" />
          <UIcon v-else :name="row.icon" class="w-4 h-4 shrink-0 text-gray-400 dark:text-gray-500" />
          <div class="flex-1 min-w-0">
            <div :id="`agent-clone-label-${row.key}`" class="text-[13px] font-medium text-gray-800 dark:text-gray-200">{{ $t(`agentClone.parts.${row.key}`) }}</div>
            <div class="text-[11px] text-gray-500 dark:text-gray-400 truncate" :data-testid="`agent-clone-hint-${row.key}`">{{ $t(`agentClone.hints.${row.key}.${choice[row.key]}`) }}</div>
          </div>
          <div role="radiogroup" :aria-labelledby="`agent-clone-label-${row.key}`" class="flex shrink-0 gap-0.5 p-0.5 bg-gray-100 dark:bg-gray-800 rounded-md">
            <button
              v-for="opt in row.options"
              :key="opt.value"
              type="button"
              role="radio"
              :aria-checked="choice[row.key] === opt.value"
              :disabled="opt.disabled"
              :title="opt.disabled ? $t(`agentClone.unavailable.${row.key}.${opt.value}`) : undefined"
              :data-testid="`agent-clone-${row.key}-${opt.value}`"
              class="px-2 py-1 text-[11px] rounded whitespace-nowrap transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              :class="choice[row.key] === opt.value ? 'bg-white dark:bg-gray-900 text-gray-900 dark:text-white shadow-sm font-medium' : 'text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'"
              @click="choice[row.key] = opt.value"
            >{{ $t(`agentClone.choices.${opt.label}`) }}</button>
          </div>
        </div>
      </div>

      <p class="mt-3 text-[11px] text-gray-500 dark:text-gray-400">{{ $t('agentClone.footnote') }}</p>

      <div v-if="blocked.length" class="mt-3 rounded-md bg-red-50 dark:bg-red-950/30 px-3 py-2 text-[11px] text-red-700 dark:text-red-300" data-testid="agent-clone-blocked">
        <p>{{ blockedMessage }}</p>
        <ul class="mt-1 list-disc ps-4">
          <li v-for="b in blocked" :key="b" dir="auto">{{ b }}</li>
        </ul>
      </div>

      <div class="flex justify-end gap-2 mt-4">
        <button type="button" class="px-3 py-1.5 text-xs border border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-300 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800/50" @click="emit('update:modelValue', false)">{{ $t('agentsPage.cancel') }}</button>
        <button
          type="submit"
          data-testid="agent-clone-submit"
          :disabled="busy || !name.trim()"
          class="px-3 py-1.5 text-xs rounded-lg text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-50 inline-flex items-center gap-1.5"
        ><Spinner v-if="busy" class="w-3 h-3" />{{ busy ? $t('agentClone.cloning') : $t('agentClone.submit') }}</button>
      </div>
    </form>
  </UModal>
</template>

<script setup lang="ts">
import { nextTick, reactive, ref, watch } from 'vue'
import Spinner from '~/components/Spinner.vue'
import LibraryIcon from '~/components/icons/LibraryIcon.vue'
import { useMyFetch } from '~/composables/useMyFetch'

type Part = 'instructions' | 'saved_queries' | 'lists' | 'evals' | 'files' | 'access'
interface Option { value: string; label: string; disabled?: boolean }

const props = defineProps<{ modelValue: boolean; agent: { id: string; name: string; description?: string | null } | null }>()
const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'finished', id: string): void
}>()
const { t } = useI18n()
const toast = useToast()

const COPY = { value: 'copy', label: 'copy' }
const LINK = { value: 'link', label: 'link' }
const NONE = { value: 'none', label: 'none' }
// Icons match the agent tree's rows for the same things.
const rows: { key: Part; icon: string; options: Option[] }[] = [
  { key: 'instructions', icon: 'i-heroicons-document-text', options: [COPY, LINK, NONE] },
  // Shared saved queries need per-agent query runs, which aren't in yet.
  { key: 'saved_queries', icon: '', options: [COPY, { ...LINK, disabled: true }, NONE] },
  { key: 'lists', icon: 'i-heroicons-list-bullet', options: [COPY, LINK, NONE] },
  { key: 'evals', icon: 'i-heroicons-check-circle', options: [COPY, NONE] },
  { key: 'files', icon: 'i-heroicons-paper-clip', options: [LINK, NONE] },
  { key: 'access', icon: 'i-heroicons-user-group', options: [{ value: 'copy', label: 'sameAccess' }, { value: 'default', label: 'onlyMe' }] },
]
const ALWAYS = ['settings', 'tables', 'tools', 'prompts']
const DEFAULTS: Record<Part, string> = {
  instructions: 'copy', saved_queries: 'none', lists: 'copy', evals: 'copy', files: 'link', access: 'default',
}

const name = ref('')
const description = ref('')
const choice = reactive<Record<Part, string>>({ ...DEFAULTS })
const busy = ref(false)
const nameError = ref('')
const blocked = ref<string[]>([])
const blockedMessage = ref('')
const nameInput = ref<HTMLInputElement | null>(null)

watch(() => props.modelValue, async (open) => {
  if (!open) return
  name.value = t('agentClone.defaultName', { name: props.agent?.name || '' })
  description.value = props.agent?.description || ''
  Object.assign(choice, DEFAULTS)
  nameError.value = ''
  blocked.value = []
  busy.value = false
  await nextTick()
  nameInput.value?.select()
})

// Names of what wasn't carried over (the server says why per item).
function skippedNames(summary: Record<string, { skipped?: { name?: string | null }[] }>) {
  const names = Object.values(summary || {}).flatMap(p => (p.skipped || []).map(s => s.name || '?'))
  return names.length > 3 ? [...names.slice(0, 3), '…'] : names
}

async function submit() {
  if (!props.agent || !name.value.trim()) return
  busy.value = true
  nameError.value = ''
  blocked.value = []
  try {
    const { data, error } = await useMyFetch<any>(`/api/data_sources/${props.agent.id}/clone`, {
      method: 'POST',
      body: { name: name.value.trim(), description: description.value, ...choice },
    })
    if (error.value) {
      const status = (error.value as any)?.statusCode ?? (error.value as any)?.status
      const detail = (error.value as any)?.data?.detail
      if (status === 409 && detail?.code === 'instructions_changed') throw new Error(t('agentClone.instructionsChanged'))
      if (status === 409) { nameError.value = t('agentClone.nameTaken'); return }
      if (detail && typeof detail === 'object' && Array.isArray(detail.instructions)) {
        blocked.value = detail.instructions
        blockedMessage.value = t('agentClone.linkBlocked')
        return
      }
      throw new Error(typeof detail === 'string' ? detail : t('agentClone.failed'))
    }
    const skipped = skippedNames(data.value?.summary)
    toast.add({
      title: t('agentClone.done', { name: data.value?.data_source?.name }),
      description: skipped.length ? t('agentClone.notCopied', { names: skipped.join(', ') }) : undefined,
      color: 'green',
    })
    emit('update:modelValue', false)
    emit('finished', data.value.data_source.id)
  } catch (e: any) {
    toast.add({ title: t('agentClone.failed'), description: e?.message, color: 'red' })
  } finally {
    busy.value = false
  }
}
</script>
