<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-lg' }">
    <UCard :ui="{ body: { padding: 'px-5 py-4 sm:p-5' }, header: { padding: 'px-5 py-3 sm:px-5 sm:py-3' }, footer: { padding: 'px-5 py-3 sm:px-5 sm:py-3' } }">
      <template #header>
        <div class="flex items-center justify-between">
          <h3 class="text-sm font-semibold text-gray-900">Subscribe to "{{ prompt?.title }}"</h3>
          <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" size="xs" @click="isOpen = false" />
        </div>
      </template>

      <div class="space-y-4">
        <!-- Schedule -->
        <PromptSchedulePicker v-model="cron" />

        <!-- Channel -->
        <div>
          <div class="text-xs text-gray-500 mb-1.5">Channel</div>
          <select v-model="channel" class="w-full rounded border border-gray-200 px-2 py-1.5 text-sm">
            <option v-for="c in PROMPT_CHANNELS" :key="c.value" :value="c.value">{{ c.label }}</option>
          </select>
        </div>

        <!-- Run mode -->
        <div>
          <div class="text-xs text-gray-500 mb-1.5">Run mode</div>
          <div class="flex gap-2">
            <button
              v-for="rm in PROMPT_RUN_MODES"
              :key="rm.value"
              type="button"
              class="flex-1 px-3 py-2 text-xs rounded border transition-colors"
              :class="runMode === rm.value ? 'border-blue-500 bg-blue-50 text-blue-700 font-medium' : 'border-gray-200 text-gray-600 hover:border-gray-300'"
              @click="runMode = rm.value"
            >
              {{ rm.label }}
            </button>
          </div>
        </div>
      </div>

      <template #footer>
        <div class="flex justify-end gap-2">
          <UButton color="gray" variant="ghost" size="xs" @click="isOpen = false">Cancel</UButton>
          <UButton color="blue" size="xs" :loading="isSaving" @click="submit">Subscribe</UButton>
        </div>
      </template>
    </UCard>
  </UModal>
</template>

<script setup lang="ts">
import PromptSchedulePicker from '@/components/prompt/PromptSchedulePicker.vue'
import { PROMPT_CHANNELS, PROMPT_RUN_MODES, type PromptChannel, type PromptRunMode, type PromptResponse } from '@/composables/usePrompts'

const props = defineProps<{ prompt: PromptResponse | null }>()
const emit = defineEmits(['subscribed'])

const isOpen = defineModel<boolean>({ default: false })
const toast = useToast()
const { subscribePrompt } = usePrompts()

const cron = ref<string>('0 8 * * *')
const channel = ref<PromptChannel>('slack')
const runMode = ref<PromptRunMode>('append')
const isSaving = ref(false)

// Seed defaults from the prompt whenever the modal opens for a new prompt.
watch(() => [props.prompt, isOpen.value], () => {
  if (!isOpen.value || !props.prompt) return
  cron.value = props.prompt.default_cron || '0 8 * * *'
  channel.value = (props.prompt.default_channel as PromptChannel) || 'slack'
  runMode.value = 'append'
})

async function submit() {
  if (!props.prompt) return
  isSaving.value = true
  try {
    await subscribePrompt(props.prompt.id, {
      cron_schedule: cron.value,
      channel: channel.value,
      run_mode: runMode.value,
    })
    toast.add({ title: 'Subscribed', description: 'You will receive scheduled runs of this prompt.', color: 'green' })
    isOpen.value = false
    emit('subscribed')
  } catch (e: any) {
    toast.add({ title: 'Failed to subscribe', description: e?.data?.detail || e?.message, color: 'red' })
  } finally {
    isSaving.value = false
  }
}
</script>
