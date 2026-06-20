<template>
  <div class="space-y-4">
    <p class="text-xs text-gray-500 max-w-md">
      Decide how this agent improves itself when a new instruction suggestion comes in
      (from the knowledge harness or a teammate).
    </p>

    <!-- Mode dropdown -->
    <div>
      <label class="block text-sm font-medium text-gray-900 mb-1.5">When a new suggestion comes in…</label>
      <select v-model="form.mode" :disabled="!canManage" @change="markDirty"
        class="w-full h-9 px-2.5 text-sm bg-white border border-gray-200 rounded-md outline-none focus:border-gray-400">
        <option value="off">Wait for review</option>
        <option value="auto_approve">Auto-approve (no evals)</option>
        <option value="eval_review" :disabled="!hasEvals">Run evals, then I approve{{ hasEvals ? '' : ' — no evals yet' }}</option>
        <option value="eval_auto" :disabled="!hasEvals">Run evals &amp; auto-approve{{ hasEvals ? '' : ' — no evals yet' }}</option>
      </select>
      <p v-if="!hasEvals" class="mt-1.5 text-[11px] text-gray-400">
        Add eval test cases for this agent to enable the “Run evals…” modes.
      </p>
    </div>

    <!-- Effective behavior summary -->
    <div class="flex items-start gap-2 rounded-md bg-blue-50 border border-blue-100 px-3 py-2">
      <UIcon name="i-heroicons-information-circle" class="w-4 h-4 text-blue-500 shrink-0 mt-0.5" />
      <p class="text-[11px] text-blue-800 leading-relaxed">{{ summary }}</p>
    </div>

    <!-- Advanced — only relevant when evals run and auto-promote -->
    <div v-if="form.mode === 'eval_auto'" class="rounded-md border border-gray-200 divide-y divide-gray-100">
      <div class="px-3 py-2 text-[10px] uppercase tracking-wide text-gray-400">Advanced</div>
      <div class="flex items-start justify-between px-3 py-2.5">
        <div class="pe-4">
          <div class="text-xs font-medium text-gray-800">Auto-fix on failure</div>
          <div class="text-[11px] text-gray-500 max-w-sm">When evals fail, draft instructions to fix them and re-eval. Off ⇒ the suggestion stays in Review marked “eval failed” (main untouched).</div>
        </div>
        <UToggle v-model="form.auto_fix_on_failure" :disabled="!canManage" @update:model-value="markDirty" />
      </div>
      <div v-if="form.auto_fix_on_failure" class="flex items-center justify-between px-3 py-2.5">
        <div class="pe-4">
          <div class="text-xs font-medium text-gray-800">On repeated failure</div>
          <div class="text-[11px] text-gray-500">When the fix loop can’t reach green.</div>
        </div>
        <select v-model="form.on_repeated_failure" :disabled="!canManage" @change="markDirty" class="h-7 px-2 text-xs bg-white border border-gray-200 rounded-md outline-none focus:border-gray-400 shrink-0">
          <option value="training">Move to training (keeps serving)</option>
          <option value="development">Move to development (pull from users)</option>
        </select>
      </div>
      <div v-if="form.auto_fix_on_failure" class="flex items-center justify-between px-3 py-2.5">
        <div class="pe-4">
          <div class="text-xs font-medium text-gray-800">Max training iterations</div>
          <div class="text-[11px] text-gray-500">Cap on the train → re-eval loop. Guards cost.</div>
        </div>
        <input v-model.number="form.max_iterations" type="number" min="1" max="10" :disabled="!canManage" @change="markDirty" class="h-7 w-16 px-2 text-xs bg-white border border-gray-200 rounded-md outline-none focus:border-gray-400 shrink-0" />
      </div>
    </div>

    <div v-if="canManage" class="flex items-center gap-2">
      <UButton color="blue" size="xs" :disabled="!dirty" :loading="savingSettings" @click="saveSettings">Save</UButton>
      <UButton color="gray" variant="ghost" size="xs" :disabled="!dirty" @click="loadAutomation">Reset</UButton>
      <span v-if="savedOk" class="text-xs text-green-600">Saved</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useCan } from '~/composables/usePermissions'
const props = defineProps<{ agentId: string | null }>()
const emit = defineEmits<{ (e: 'saved'): void }>()
const toast = useToast()
const canManage = computed(() => props.agentId ? useCan('manage', { type: 'data_source', id: props.agentId }) : false)

const defaultForm = () => ({
  mode: 'off',
  auto_fix_on_failure: false,
  on_repeated_failure: 'training',
  max_iterations: 3,
})
const form = ref<Record<string, any>>(defaultForm())
const evalCaseCount = ref(0)
const hasEvals = computed(() => evalCaseCount.value > 0)
const dirty = ref(false)
const savingSettings = ref(false)
const savedOk = ref(false)

function markDirty() { dirty.value = true; savedOk.value = false }

const summary = computed(() => {
  const f = form.value
  if (f.mode === 'auto_approve') return 'New suggestions are promoted to this agent immediately — no evals, no human review.'
  if (f.mode === 'eval_review') return 'Each suggestion is evaluated on a candidate build; a passing candidate waits for your approval.'
  if (f.mode === 'eval_auto') return 'Each suggestion is evaluated on a candidate build and auto-promoted when the evals pass' + (f.auto_fix_on_failure ? '; on failure a fix loop runs.' : '; on failure it stays in Review.')
  return 'New suggestions wait in the Review feed for a human. Nothing runs automatically.'
})

async function loadAutomation() {
  const id = props.agentId
  if (!id) return
  try {
    const res = await useMyFetch<any>(`/data_sources/${id}/automation`, { method: 'GET' })
    const data: any = res.data.value
    if (!data) return
    form.value = { ...defaultForm(), ...(data.effective || {}) }
    evalCaseCount.value = data.eval_case_count || 0
    dirty.value = false; savedOk.value = false
  } catch (e) { /* noop */ }
}
async function saveSettings() {
  const id = props.agentId
  if (!id) return
  savingSettings.value = true; savedOk.value = false
  try {
    await useMyFetch(`/data_sources/${id}/automation`, { method: 'PATCH', body: { ...form.value } })
    savedOk.value = true; dirty.value = false; emit('saved'); await loadAutomation()
  } catch (e) { toast.add({ title: 'Failed to save Self Learning settings', color: 'red' }) }
  finally { savingSettings.value = false }
}

watch(() => props.agentId, loadAutomation)
onMounted(loadAutomation)
</script>
