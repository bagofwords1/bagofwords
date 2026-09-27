<template>
  <div class="mt-1" data-testid="suggest-instruction-tool">
    <div class="flex items-center text-xs text-gray-500 dark:text-gray-400">
      <span v-if="status === 'running'" class="tool-shimmer flex items-center">
        <Icon name="heroicons-light-bulb" class="w-3 h-3 me-1.5 text-gray-400" />
        <span dir="auto" class="truncate max-w-[300px]">{{ title || $t('tools.suggestInstruction.running') }}</span>
      </span>
      <span v-else-if="!isSuccess" class="flex items-center">
        <Icon name="heroicons-x-circle" class="w-3 h-3 me-1.5 text-red-500" />
        <span>{{ $t('tools.suggestInstruction.failed') }}</span>
      </span>
    </div>

    <!-- The proposed rule: the user decides; nothing is saved until they click. -->
    <div
      v-if="isSuccess && rule"
      class="mt-1 rounded-md border border-amber-200/70 dark:border-amber-900/60 bg-amber-50/50 dark:bg-amber-950/20 px-3 py-2 max-w-[560px]"
    >
      <div class="text-[11px] text-gray-500 dark:text-gray-400 flex items-center gap-1 mb-1">
        <Icon name="heroicons-light-bulb" class="w-3 h-3 text-amber-500" />
        {{ $t('tools.suggestInstruction.label') }}
      </div>
      <div class="text-[13px] text-gray-800 dark:text-gray-200" dir="auto" data-testid="suggest-instruction-text">{{ rule }}</div>
      <div class="mt-2 flex items-center gap-2">
        <span v-if="state === 'saved'" class="text-[11px] text-green-700 dark:text-green-400 flex items-center gap-1" data-testid="suggest-instruction-saved">
          <Icon name="heroicons-check-circle" class="w-3.5 h-3.5" />{{ $t('tools.suggestInstruction.saved') }}
        </span>
        <template v-else-if="!readonly">
          <UButton size="2xs" color="gray" variant="solid" :loading="state === 'saving'" data-testid="suggest-instruction-accept" @click="accept">
            {{ $t('tools.suggestInstruction.accept') }}
          </UButton>
          <span v-if="state === 'dismissed'" class="text-[11px] text-gray-400">{{ $t('tools.suggestInstruction.dismissed') }}</span>
          <button v-else type="button" class="text-[11px] text-gray-400 hover:text-gray-600" @click="state = 'dismissed'">
            {{ $t('tools.suggestInstruction.dismiss') }}
          </button>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

interface Props {
  toolExecution: {
    tool_name: string
    arguments_json?: { text?: string; title?: string }
    result_json?: { success?: boolean; text?: string; already_saved?: boolean }
    status: string
  }
  readonly?: boolean
}
const props = defineProps<Props>()
const { t } = useI18n()
const toast = useToast()
const { getErrorMessage } = useErrorMessage()

const status = computed(() => props.toolExecution.status)
const isSuccess = computed(() => status.value === 'success' && props.toolExecution.result_json?.success !== false)
const title = computed(() => props.toolExecution.arguments_json?.title || '')
const rule = computed(() => props.toolExecution.result_json?.text || props.toolExecution.arguments_json?.text || '')
const state = ref<'idle' | 'saving' | 'saved' | 'dismissed'>(props.toolExecution.result_json?.already_saved ? 'saved' : 'idle')

const norm = (s: string) => s.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim()

// Reloading the report after accepting shows the saved state again.
onMounted(async () => {
  if (state.value !== 'idle' || props.readonly || !rule.value) return
  try {
    const res = await useMyFetch('/users/me/instructions')
    const note = ((res.data?.value as any)?.note || '') as string
    const key = norm(rule.value)
    if (note.split('\n').some(l => norm(l.replace(/^\s*[-*•]\s*/, '')) === key)) state.value = 'saved'
  } catch { /* non-fatal */ }
})

async function accept() {
  state.value = 'saving'
  try {
    const res = await useMyFetch('/users/me/instructions/rules', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: rule.value }),
    })
    if (res.status.value !== 'success') throw res.error.value
    state.value = 'saved'
  } catch (e) {
    state.value = 'idle'
    toast.add({ title: getErrorMessage(e, t('tools.suggestInstruction.failedSave')), color: 'red' })
  }
}
</script>
