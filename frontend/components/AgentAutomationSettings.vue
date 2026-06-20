<template>
  <div class="space-y-5">
    <p class="text-xs text-gray-500 max-w-md">
      Decide how much this agent improves itself when a new instruction suggestion comes in
      (from the knowledge harness or a teammate). Walk the questions top to bottom.
    </p>

    <!-- Q1: auto-approve suggestions -->
    <div class="rounded-md border border-gray-200 divide-y divide-gray-100">
      <div class="flex items-start justify-between px-3 py-3">
        <div class="pe-4">
          <div class="text-sm font-medium text-gray-900">Auto-approve suggestions?</div>
          <div class="text-[11px] text-gray-500 mt-0.5 max-w-sm">
            Promote new suggestions to this agent <span class="font-medium">immediately</span>, without running evals.
          </div>
        </div>
        <UToggle v-model="form.auto_approve_suggestions" :disabled="!canManage" @update:model-value="onAutoApprove" />
      </div>

      <!-- Q2: auto-run eval — implies auto-approve on a green run -->
      <div v-if="!form.auto_approve_suggestions" class="flex items-start justify-between px-3 py-3">
        <div class="pe-4">
          <div class="text-sm font-medium text-gray-900">Auto-run eval &amp; approve on pass?</div>
          <div class="text-[11px] text-gray-500 mt-0.5 max-w-sm">
            Evaluate each suggestion against a candidate build (main + the new hunks) and promote it automatically if the evals pass. Otherwise it waits in Review.
          </div>
        </div>
        <UToggle v-model="form.auto_run_eval" :disabled="!canManage" @update:model-value="markDirty" />
      </div>
    </div>

    <!-- Effective behavior summary -->
    <div class="flex items-start gap-2 rounded-md bg-blue-50 border border-blue-100 px-3 py-2">
      <UIcon name="i-heroicons-information-circle" class="w-4 h-4 text-blue-500 shrink-0 mt-0.5" />
      <p class="text-[11px] text-blue-800 leading-relaxed">{{ summary }}</p>
    </div>

    <!-- Advanced -->
    <div>
      <button type="button" class="flex items-center gap-1 text-xs font-medium text-gray-500 hover:text-gray-800" @click="showAdvanced = !showAdvanced">
        <UIcon :name="showAdvanced ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" class="w-3 h-3" />
        Advanced
      </button>
      <div v-if="showAdvanced" class="mt-2 rounded-md border border-gray-200 divide-y divide-gray-100">
        <div class="flex items-start justify-between px-3 py-2.5">
          <div class="pe-4">
            <div class="text-xs font-medium text-gray-800">Auto-fix on failure</div>
            <div class="text-[11px] text-gray-500 max-w-sm">When evals fail, draft instructions to fix them and re-eval. Off ⇒ the suggestion just stays in Review marked "eval failed" (main untouched).</div>
          </div>
          <UToggle v-model="form.auto_fix_on_failure" :disabled="!canManage || !form.auto_run_eval" @update:model-value="markDirty" />
        </div>
        <div class="flex items-center justify-between px-3 py-2.5">
          <div class="pe-4">
            <div class="text-xs font-medium text-gray-800">On repeated failure</div>
            <div class="text-[11px] text-gray-500">When the fix loop can't reach green.</div>
          </div>
          <select v-model="form.on_repeated_failure" :disabled="!canManage" @change="markDirty" class="h-7 px-2 text-xs bg-white border border-gray-200 rounded-md outline-none focus:border-gray-400 shrink-0">
            <option value="training">Move to training (keeps serving)</option>
            <option value="development">Move to development (pull from users)</option>
          </select>
        </div>
        <div class="flex items-center justify-between px-3 py-2.5">
          <div class="pe-4">
            <div class="text-xs font-medium text-gray-800">Max training iterations</div>
            <div class="text-[11px] text-gray-500">Cap on the train → re-eval loop. Guards cost.</div>
          </div>
          <input v-model.number="form.max_iterations" type="number" min="1" max="10" :disabled="!canManage" @change="markDirty" class="h-7 w-16 px-2 text-xs bg-white border border-gray-200 rounded-md outline-none focus:border-gray-400 shrink-0" />
        </div>
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
// Real per-agent permission (mirrors PublishStatusControl). NB: do NOT take this
// as a Boolean prop — Vue defaults an absent Boolean prop to false, which would
// silently disable every control.
const canManage = computed(() => props.agentId ? useCan('manage', { type: 'data_source', id: props.agentId }) : false)

const showAdvanced = ref(false)

// The four-toggle Self-Learning decision tree (mirrors AgentAutomationPolicy).
const defaultForm = () => ({
  auto_approve_suggestions: false,
  auto_run_eval: false,
  auto_fix_on_failure: false,
  on_repeated_failure: 'training',
  max_iterations: 3,
})
const form = ref<Record<string, any>>(defaultForm())
const dirty = ref(false)
const savingSettings = ref(false)
const savedOk = ref(false)

function markDirty() { dirty.value = true; savedOk.value = false }
// Auto-approve short-circuits the eval branch — clear it so we never persist a
// contradictory (auto-approve + auto-eval) state.
function onAutoApprove(v: boolean) {
  if (v) { form.value.auto_run_eval = false }
  markDirty()
}

const summary = computed(() => {
  const f = form.value
  if (f.auto_approve_suggestions) return 'New suggestions are promoted to this agent immediately — no evals, no human review.'
  if (!f.auto_run_eval) return 'New suggestions wait in the Review feed for a human. Nothing runs automatically.'
  return 'Each suggestion is evaluated on a candidate build and auto-promoted if the evals pass' + (f.auto_fix_on_failure ? '; on failure a fix loop runs.' : '; on failure it stays in Review.')
})

async function loadAutomation() {
  const id = props.agentId
  if (!id) return
  try {
    const res = await useMyFetch<any>(`/data_sources/${id}/automation`, { method: 'GET' })
    const data: any = res.data.value
    if (!data) return
    form.value = { ...defaultForm(), ...(data.effective || {}) }
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
