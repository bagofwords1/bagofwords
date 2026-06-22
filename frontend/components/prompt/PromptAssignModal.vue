<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-lg' }">
    <UCard :ui="{ body: { padding: 'px-5 py-4 sm:p-5' }, header: { padding: 'px-5 py-3 sm:px-5 sm:py-3' }, footer: { padding: 'px-5 py-3 sm:px-5 sm:py-3' } }">
      <template #header>
        <div class="flex items-center justify-between">
          <h3 class="text-sm font-semibold text-gray-900">Assign "{{ prompt?.title }}"</h3>
          <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" size="xs" @click="isOpen = false" />
        </div>
      </template>

      <div class="space-y-4">
        <!-- Principal type -->
        <div>
          <div class="text-xs text-gray-500 mb-1.5">Assign to</div>
          <div class="flex gap-0.5 p-0.5 bg-gray-100 rounded w-fit">
            <button
              v-for="pt in principalTypes"
              :key="pt.value"
              type="button"
              class="px-2.5 py-1 text-[11px] rounded transition-colors"
              :class="principalType === pt.value ? 'bg-white text-gray-900 shadow-sm font-medium' : 'text-gray-400 hover:text-gray-600'"
              @click="principalType = pt.value"
            >
              {{ pt.label }}
            </button>
          </div>
        </div>

        <!-- Principal selector -->
        <div v-if="principalType === 'user'">
          <div class="text-xs text-gray-500 mb-1.5">User</div>
          <select v-if="members.length" v-model="principalId" class="w-full rounded border border-gray-200 px-2 py-1.5 text-sm">
            <option value="" disabled>Select a user…</option>
            <option v-for="m in members" :key="m.id" :value="m.id">{{ m.name || m.email }}</option>
          </select>
          <!-- TODO: fallback free-text id if the members endpoint returned nothing. -->
          <input v-else v-model="principalId" type="text" placeholder="User ID" class="w-full rounded border border-gray-200 px-2 py-1.5 text-sm" />
        </div>

        <div v-else-if="principalType === 'group'">
          <div class="text-xs text-gray-500 mb-1.5">Group</div>
          <select v-if="groups.length" v-model="principalId" class="w-full rounded border border-gray-200 px-2 py-1.5 text-sm">
            <option value="" disabled>Select a group…</option>
            <option v-for="g in groups" :key="g.id" :value="g.id">{{ g.name }}</option>
          </select>
          <!-- TODO: fallback free-text id if the groups endpoint returned nothing. -->
          <input v-else v-model="principalId" type="text" placeholder="Group ID" class="w-full rounded border border-gray-200 px-2 py-1.5 text-sm" />
        </div>

        <div v-else class="text-xs text-gray-500">
          This will assign the prompt to <span class="font-medium text-gray-700">all users</span> in the organization.
        </div>

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

        <!-- Result summary -->
        <div v-if="result" class="text-xs rounded border border-green-200 bg-green-50 px-3 py-2 text-green-700">
          Created {{ result.created }} subscription(s), skipped {{ result.skipped }}.
        </div>
      </div>

      <template #footer>
        <div class="flex justify-end gap-2">
          <UButton color="gray" variant="ghost" size="xs" @click="isOpen = false">Close</UButton>
          <UButton color="blue" size="xs" :loading="isSaving" :disabled="!canSubmit" @click="submit">Assign</UButton>
        </div>
      </template>
    </UCard>
  </UModal>
</template>

<script setup lang="ts">
import PromptSchedulePicker from '@/components/prompt/PromptSchedulePicker.vue'
import { PROMPT_CHANNELS, PROMPT_RUN_MODES, type PromptChannel, type PromptRunMode, type PrincipalType, type PromptResponse, type AssignResult } from '@/composables/usePrompts'

const props = defineProps<{ prompt: PromptResponse | null }>()
const emit = defineEmits(['assigned'])

const isOpen = defineModel<boolean>({ default: false })
const toast = useToast()
const { assignPrompt } = usePrompts()
const { organization } = useOrganization()

const principalTypes: { value: PrincipalType; label: string }[] = [
  { value: 'user', label: 'User' },
  { value: 'group', label: 'Group' },
  { value: 'org', label: 'All users' },
]

const principalType = ref<PrincipalType>('user')
const principalId = ref<string>('')
const cron = ref<string>('0 8 * * *')
const channel = ref<PromptChannel>('slack')
const runMode = ref<PromptRunMode>('append')
const isSaving = ref(false)
const result = ref<AssignResult | null>(null)

const members = ref<{ id: string; name: string; email: string }[]>([])
const groups = ref<{ id: string; name: string }[]>([])

const canSubmit = computed(() => {
  if (principalType.value === 'org') return true
  return !!principalId.value
})

watch(() => [props.prompt, isOpen.value], () => {
  if (!isOpen.value || !props.prompt) return
  cron.value = props.prompt.default_cron || '0 8 * * *'
  channel.value = (props.prompt.default_channel as PromptChannel) || 'slack'
  runMode.value = 'append'
  result.value = null
  principalId.value = ''
})

watch(isOpen, (open) => { if (open) loadPrincipals() })

async function loadPrincipals() {
  const orgId = organization.value?.id
  if (!orgId) return
  try {
    const [mRes, gRes] = await Promise.all([
      useMyFetch(`/organizations/${orgId}/members`),
      useMyFetch(`/organizations/${orgId}/groups`),
    ])
    if (mRes.data.value) {
      members.value = (mRes.data.value as any[]).map((u: any) => ({
        id: u.id || u.user_id,
        name: u.name || '',
        email: u.email || '',
      }))
    }
    if (gRes.data.value) {
      const raw = Array.isArray(gRes.data.value) ? gRes.data.value : (gRes.data.value as any).groups || []
      groups.value = raw.map((g: any) => ({ id: g.id, name: g.name }))
    }
  } catch {
    // Endpoints failed: selectors fall back to free-text id inputs (see template).
  }
}

async function submit() {
  if (!props.prompt || !canSubmit.value) return
  isSaving.value = true
  try {
    const body: any = {
      principal_type: principalType.value,
      cron_schedule: cron.value,
      channel: channel.value,
      run_mode: runMode.value,
    }
    if (principalType.value !== 'org') body.principal_id = principalId.value
    result.value = await assignPrompt(props.prompt.id, body)
    toast.add({
      title: 'Assigned',
      description: `Created ${result.value.created}, skipped ${result.value.skipped}.`,
      color: 'green',
    })
    emit('assigned', result.value)
  } catch (e: any) {
    toast.add({ title: 'Failed to assign', description: e?.data?.detail || e?.message, color: 'red' })
  } finally {
    isSaving.value = false
  }
}
</script>
