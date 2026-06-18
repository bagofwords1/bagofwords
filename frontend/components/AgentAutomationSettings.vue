<template>
  <div class="space-y-5">
    <!-- Automation mode = master switch + autonomy in one control -->
    <div class="flex items-start justify-between">
      <div class="pe-4">
        <div class="text-sm font-medium text-gray-900">Automation</div>
        <div class="text-xs text-gray-500 mt-0.5 max-w-sm">{{ modeHelp }}</div>
      </div>
      <USelectMenu v-model="mode" :options="modeOptions" value-attribute="value" option-attribute="label"
        :disabled="!canManage" size="sm" class="w-44 shrink-0" @update:model-value="applyMode" />
    </div>

    <!-- Advanced: per-stage control -->
    <div :class="{ 'opacity-50 pointer-events-none': !form.enabled }">
      <button type="button" class="flex items-center gap-1 text-xs font-medium text-gray-500 hover:text-gray-800" @click="showAdvanced = !showAdvanced">
        <UIcon :name="showAdvanced ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" class="w-3 h-3" />
        Advanced
      </button>
      <div v-if="showAdvanced" class="mt-2">
        <p class="text-[11px] text-gray-400 mb-2">Each stage: <span class="font-medium">Off</span> · <span class="font-medium">Suggest</span> (stop for review) · <span class="font-medium">Auto</span> (end to end).</p>
        <div class="rounded-md border border-gray-200 divide-y divide-gray-100">
          <div class="flex items-center justify-between px-3 py-2.5">
            <div class="pe-4">
              <div class="text-xs font-medium text-gray-800">Re-run evals when things change</div>
              <div class="text-[11px] text-gray-500">Tables activated/changed, this agent's instructions, or a global build promoted.</div>
            </div>
            <select v-model="evalTrigger" :disabled="!canManage" class="h-7 px-2 text-xs bg-white border border-gray-200 rounded-md outline-none focus:border-gray-400 shrink-0">
              <option v-for="o in AUTONOMY_OPTS" :key="o.value" :value="o.value">{{ o.label }}</option>
            </select>
          </div>
          <div v-for="dial in advancedDials" :key="dial.key" class="flex items-center justify-between px-3 py-2.5">
            <div class="pe-4">
              <div class="text-xs font-medium text-gray-800">{{ dial.label }}</div>
              <div class="text-[11px] text-gray-500">{{ dial.help }}</div>
            </div>
            <select v-model="(form as any)[dial.key]" :disabled="!canManage" @change="onAdvancedChange" class="h-7 px-2 text-xs bg-white border border-gray-200 rounded-md outline-none focus:border-gray-400 shrink-0">
              <option v-for="o in dial.options" :key="o.value" :value="o.value">{{ o.label }}</option>
            </select>
          </div>
          <div class="flex items-center justify-between px-3 py-2.5">
            <div class="pe-4">
              <div class="text-xs font-medium text-gray-800">On repeated failure</div>
              <div class="text-[11px] text-gray-500">When the loop can't reach green: keep it in training (still visible) or move it to development (only admins).</div>
            </div>
            <select v-model="form.on_repeated_failure" :disabled="!canManage" @change="markDirty" class="h-7 px-2 text-xs bg-white border border-gray-200 rounded-md outline-none focus:border-gray-400 shrink-0">
              <option value="none">Do nothing</option>
              <option value="training">Keep in training</option>
              <option value="development">Move to development</option>
            </select>
          </div>
          <div class="flex items-center justify-between px-3 py-2.5">
            <div class="pe-4">
              <div class="text-xs font-medium text-gray-800">Max training iterations</div>
              <div class="text-[11px] text-gray-500">Cap on the train → re-eval loop before giving up. Guards cost.</div>
            </div>
            <input v-model.number="form.max_iterations" type="number" min="1" max="10" :disabled="!canManage" @change="markDirty" class="h-7 w-16 px-2 text-xs bg-white border border-gray-200 rounded-md outline-none focus:border-gray-400 shrink-0" />
          </div>
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
const props = defineProps<{ agentId: string | null; canManage?: boolean }>()
const emit = defineEmits<{ (e: 'saved'): void }>()
const toast = useToast()

const AUTONOMY_OPTS = [
  { value: 'off', label: 'Off' },
  { value: 'suggest', label: 'Suggest' },
  { value: 'auto', label: 'Auto' },
]
const advancedDials = [
  { key: 'train_on_failure', label: 'Train on failure', help: 'When evals fail, draft instructions that fix them.', options: AUTONOMY_OPTS },
  { key: 'approve_instructions', label: 'Approve instructions', help: 'Push a passing build live. Auto = no human in the loop.', options: AUTONOMY_OPTS },
  { key: 'auto_promote_evals', label: 'Auto-promote thumbs-up evals', help: 'Promote auto-drafted evals straight to active.', options: AUTONOMY_OPTS },
]
const PRESETS: Record<string, any> = {
  assisted:   { trigger: 'auto', train_on_failure: 'auto', approve_instructions: 'suggest', auto_promote_evals: 'off' },
  autonomous: { trigger: 'auto', train_on_failure: 'auto', approve_instructions: 'auto',    auto_promote_evals: 'auto' },
}
type Mode = 'off' | 'assisted' | 'autonomous' | 'custom'
const mode = ref<Mode>('off')
const showAdvanced = ref(false)
const modeOptions = computed(() => {
  const base: { value: Mode; label: string }[] = [
    { value: 'off', label: 'Off' }, { value: 'assisted', label: 'Assisted' }, { value: 'autonomous', label: 'Autonomous' },
  ]
  if (mode.value === 'custom') base.push({ value: 'custom', label: 'Custom' })
  return base
})
const modeHelp = computed(() => ({
  off: 'Paused. Nothing runs automatically — fire actions from Review on demand.',
  assisted: 'Runs evals on changes and drafts fixes — you approve before anything goes live.',
  autonomous: 'Measures, fixes, and promotes end to end. No human in the loop.',
  custom: 'Custom per-stage settings — see Advanced below.',
} as Record<string, string>)[mode.value])

const defaultForm = () => ({
  enabled: false, eval_on_table_change: 'suggest', eval_on_change: 'suggest', eval_on_global_change: 'suggest',
  train_on_failure: 'suggest', approve_instructions: 'suggest', auto_promote_evals: 'off',
  on_repeated_failure: 'training', max_iterations: 3,
})
const form = ref<Record<string, any>>(defaultForm())
const dirty = ref(false)
const savingSettings = ref(false)
const savedOk = ref(false)
const canManage = computed(() => props.canManage !== false)

function markDirty() { dirty.value = true; savedOk.value = false }
function detectMode(): Mode {
  if (!form.value.enabled) return 'off'
  const f = form.value
  const triggersEqual = f.eval_on_table_change === f.eval_on_change && f.eval_on_change === f.eval_on_global_change
  if (triggersEqual) {
    for (const name of ['assisted', 'autonomous'] as const) {
      const p = PRESETS[name]
      if (f.eval_on_change === p.trigger && f.train_on_failure === p.train_on_failure && f.approve_instructions === p.approve_instructions && f.auto_promote_evals === p.auto_promote_evals) return name
    }
  }
  return 'custom'
}
function applyMode(next?: Mode) {
  const m = next ?? mode.value; mode.value = m
  if (m === 'off') { form.value.enabled = false; markDirty(); return }
  form.value.enabled = true
  const p = PRESETS[m]
  if (p) { form.value.eval_on_table_change = p.trigger; form.value.eval_on_change = p.trigger; form.value.eval_on_global_change = p.trigger; form.value.train_on_failure = p.train_on_failure; form.value.approve_instructions = p.approve_instructions; form.value.auto_promote_evals = p.auto_promote_evals }
  markDirty()
}
const evalTrigger = computed<string>({
  get: () => form.value.eval_on_change,
  set: (v: string) => { form.value.eval_on_table_change = v; form.value.eval_on_change = v; form.value.eval_on_global_change = v; onAdvancedChange() },
})
function onAdvancedChange() { markDirty(); mode.value = detectMode() }

async function loadAutomation() {
  const id = props.agentId
  if (!id) return
  try {
    const res = await useMyFetch<any>(`/data_sources/${id}/automation`, { method: 'GET' })
    const data: any = res.data.value
    if (!data) return
    form.value = { ...defaultForm(), ...(data.effective || {}) }
    mode.value = detectMode(); dirty.value = false; savedOk.value = false
  } catch (e) { /* noop */ }
}
async function saveSettings() {
  const id = props.agentId
  if (!id) return
  savingSettings.value = true; savedOk.value = false
  try {
    await useMyFetch(`/data_sources/${id}/automation`, { method: 'PATCH', body: { ...form.value } })
    savedOk.value = true; dirty.value = false; emit('saved'); await loadAutomation()
  } catch (e) { toast.add({ title: 'Failed to save automation settings', color: 'red' }) }
  finally { savingSettings.value = false }
}

watch(() => props.agentId, loadAutomation)
onMounted(loadAutomation)
</script>
