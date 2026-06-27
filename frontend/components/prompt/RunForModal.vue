<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-lg' }" :prevent-close="isRunning">
    <UCard :ui="{ body: { padding: 'px-5 py-4 sm:p-5' }, header: { padding: 'px-5 py-3 sm:px-5 sm:py-3' }, footer: { padding: 'px-5 py-3 sm:px-5 sm:py-3' } }">
      <template #header>
        <div class="flex items-center justify-between">
          <h3 class="text-sm font-semibold text-gray-900 dark:text-white">
            {{ $t('prompts.runForTitle', { title: prompt?.title || $t('prompts.untitled') }) }}
          </h3>
          <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" size="xs" @click="isOpen = false" />
        </div>
      </template>

      <div class="space-y-4">
        <!-- Target type switch -->
        <div class="flex gap-1 p-0.5 bg-gray-100 dark:bg-gray-800 rounded w-fit">
          <button
            type="button"
            class="px-2.5 py-1 text-[11px] rounded transition-colors"
            :class="targetType === 'users' ? 'bg-white dark:bg-gray-900 text-gray-900 dark:text-white shadow-sm font-medium' : 'text-gray-500 hover:text-gray-700'"
            @click="targetType = 'users'"
          >{{ $t('prompts.targetMembers') }}</button>
          <button
            type="button"
            class="px-2.5 py-1 text-[11px] rounded transition-colors"
            :class="targetType === 'group' ? 'bg-white dark:bg-gray-900 text-gray-900 dark:text-white shadow-sm font-medium' : 'text-gray-500 hover:text-gray-700'"
            @click="targetType = 'group'"
          >{{ $t('prompts.targetGroup') }}</button>
        </div>

        <!-- Members picker -->
        <div v-if="targetType === 'users'">
          <!-- Selected chips -->
          <div class="flex flex-wrap items-center gap-1 border border-gray-200 dark:border-gray-700 rounded px-2 py-1.5 min-h-[34px] focus-within:ring-1 focus-within:ring-blue-500 bg-white dark:bg-gray-900">
            <span
              v-for="m in selectedMembers"
              :key="m.id"
              class="inline-flex items-center gap-0.5 bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 text-[11px] px-1.5 py-0.5 rounded-full"
            >
              {{ m.name || m.email }}
              <button class="hover:text-red-500" @click="removeMember(m.id)">
                <UIcon name="heroicons-x-mark" class="w-2.5 h-2.5" />
              </button>
            </span>
            <div class="relative flex-1 min-w-[120px]">
              <input
                v-model="memberQuery"
                type="text"
                class="w-full border-none outline-none text-xs bg-transparent p-0"
                :placeholder="$t('prompts.searchMembers')"
                @focus="showDropdown = true"
                @input="showDropdown = true"
                @blur="onBlur"
              />
              <div
                v-if="showDropdown && filteredMembers.length"
                class="absolute start-0 top-full mt-1 w-64 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded shadow-lg z-50 max-h-40 overflow-y-auto"
              >
                <button
                  v-for="m in filteredMembers"
                  :key="m.id"
                  class="w-full text-start px-2 py-1.5 text-xs hover:bg-gray-50 dark:hover:bg-gray-800 flex flex-col"
                  @mousedown.prevent="addMember(m)"
                >
                  <span class="text-gray-900 dark:text-white">{{ m.name || m.email }}</span>
                  <span v-if="m.name" class="text-[10px] text-gray-400">{{ m.email }}</span>
                </button>
              </div>
            </div>
          </div>
        </div>

        <!-- Group picker -->
        <div v-else>
          <select
            v-model="selectedGroupId"
            class="w-full text-sm border border-gray-200 dark:border-gray-700 rounded px-2 py-2 dark:bg-gray-900 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-500"
          >
            <option :value="null">{{ $t('prompts.selectGroup') }}</option>
            <option v-for="g in groups" :key="g.id" :value="g.id">
              {{ g.name }} ({{ $t('prompts.nMembers', { n: g.member_count }) }})
            </option>
          </select>
        </div>

        <!-- Parameters note -->
        <div v-if="hasParams" class="rounded-md border border-gray-100 dark:border-gray-800 bg-gray-50/60 dark:bg-gray-800/40 p-2.5">
          <div class="flex items-center justify-between">
            <span class="text-[11px] text-gray-500 dark:text-gray-400">
              {{ paramsCollected ? $t('prompts.paramsReady') : $t('prompts.paramsNeeded') }}
            </span>
            <button
              type="button"
              class="text-[11px] text-blue-500 hover:text-blue-600"
              @click="openParams"
            >{{ paramsCollected ? $t('prompts.editParams') : $t('prompts.fillParams') }}</button>
          </div>
        </div>

        <!-- Confirmation line -->
        <div v-if="targetCount > 0" class="text-xs text-gray-600 dark:text-gray-300 rounded-md bg-blue-50 dark:bg-blue-900/20 border border-blue-100 dark:border-blue-900 p-2.5">
          <UIcon name="heroicons-information-circle" class="w-3.5 h-3.5 inline -mt-0.5 text-blue-500" />
          {{ $t('prompts.runForConfirm', { title: prompt?.title || $t('prompts.untitled'), n: targetCount }) }}
        </div>
      </div>

      <template #footer>
        <div class="flex items-center justify-end gap-2">
          <UButton color="gray" variant="ghost" size="xs" @click="isOpen = false">{{ $t('prompts.cancel') }}</UButton>
          <UButton color="blue" size="xs" :loading="isRunning" :disabled="!canRun" @click="run">
            {{ $t('prompts.runForAction') }}
          </UButton>
        </div>
      </template>
    </UCard>
  </UModal>

  <PromptParametersModal
    v-model="paramsModalOpen"
    :prompt="prompt"
    @confirm="onParamsConfirm"
  />
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import PromptParametersModal from '@/components/prompt/PromptParametersModal.vue'
import { usePrompts } from '~/composables/usePrompts'
import type { Prompt } from '~/composables/usePrompts'
import type { PromptParamValue } from '~/composables/usePromptFill'

const props = defineProps<{
  modelValue: boolean
  prompt: Prompt | null
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
}>()

const { t } = useI18n()
const toast = useToast()
const { organization } = useOrganization()
const { runPromptFor } = usePrompts()

const isOpen = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const targetType = ref<'users' | 'group'>('users')
const isRunning = ref(false)

// ── Members + groups (sourced from the org endpoints the assign UIs use) ──
interface PickMember { id: string; name: string; email: string }
interface PickGroup { id: string; name: string; member_count: number }

const allMembers = ref<PickMember[]>([])
const groups = ref<PickGroup[]>([])
const selectedMemberIds = ref<string[]>([])
const selectedGroupId = ref<string | null>(null)
const memberQuery = ref('')
const showDropdown = ref(false)

async function loadTargets() {
  const orgId = organization.value?.id
  if (!orgId) return
  try {
    const { data } = await useMyFetch<any[]>(`/organizations/${orgId}/members`)
    allMembers.value = (data.value || [])
      .map((m: any) => {
        const userId = m.user_id || m.user?.id
        return userId
          ? { id: userId, name: m.user?.name || '', email: m.user?.email || m.email || '' }
          : null
      })
      .filter(Boolean) as PickMember[]
  } catch {}
  try {
    const { data } = await useMyFetch<any[]>(`/organizations/${orgId}/groups`)
    groups.value = (data.value || []).map((g: any) => ({ id: g.id, name: g.name, member_count: g.member_count ?? 0 }))
  } catch {}
}

watch(() => props.modelValue, (open) => {
  if (open) {
    selectedMemberIds.value = []
    selectedGroupId.value = null
    memberQuery.value = ''
    targetType.value = 'users'
    collectedParams.value = null
    paramsCollected.value = false
    loadTargets()
  }
})

const selectedMembers = computed(() =>
  selectedMemberIds.value.map(id => allMembers.value.find(m => m.id === id)).filter(Boolean) as PickMember[],
)

const filteredMembers = computed(() => {
  const q = memberQuery.value.toLowerCase().trim()
  return allMembers.value
    .filter(m => !selectedMemberIds.value.includes(m.id))
    .filter(m => !q || m.email.toLowerCase().includes(q) || m.name.toLowerCase().includes(q))
    .slice(0, 6)
})

function addMember(m: PickMember) {
  if (!selectedMemberIds.value.includes(m.id)) selectedMemberIds.value.push(m.id)
  memberQuery.value = ''
  showDropdown.value = false
}
function removeMember(id: string) {
  selectedMemberIds.value = selectedMemberIds.value.filter(x => x !== id)
}
function onBlur() {
  setTimeout(() => { showDropdown.value = false }, 200)
}

// ── Parameters ──
const hasParams = computed(() => (props.prompt?.parameters || []).length > 0)
const paramsModalOpen = ref(false)
const paramsCollected = ref(false)
const collectedParams = ref<Record<string, PromptParamValue> | null>(null)

function openParams() {
  paramsModalOpen.value = true
}
function onParamsConfirm(values: Record<string, PromptParamValue>) {
  collectedParams.value = values
  paramsCollected.value = true
}

// ── Confirmation / run ──
const targetCount = computed(() => {
  if (targetType.value === 'users') return selectedMemberIds.value.length
  const g = groups.value.find(x => x.id === selectedGroupId.value)
  return g ? g.member_count : 0
})

const canRun = computed(() => {
  if (isRunning.value) return false
  if (hasParams.value && !paramsCollected.value) return false
  if (targetType.value === 'users') return selectedMemberIds.value.length > 0
  return !!selectedGroupId.value
})

async function run() {
  if (!canRun.value || !props.prompt) return
  isRunning.value = true
  try {
    const payload: any = {
      principal_type: targetType.value,
      parameters: collectedParams.value || undefined,
    }
    if (targetType.value === 'users') payload.user_ids = selectedMemberIds.value
    else payload.group_id = selectedGroupId.value

    const res = await runPromptFor(props.prompt.id, payload)
    if (res) {
      toast.add({
        title: t('prompts.runForResult', { ran: res.ran, skipped: res.skipped }),
        color: 'green',
      })
      isOpen.value = false
    } else {
      toast.add({ title: t('prompts.toastRunForFailed'), color: 'red' })
    }
  } catch (e: any) {
    toast.add({ title: e?.data?.detail || t('prompts.toastRunForFailed'), color: 'red' })
  } finally {
    isRunning.value = false
  }
}
</script>
