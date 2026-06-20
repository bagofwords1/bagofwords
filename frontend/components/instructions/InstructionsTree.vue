<template>
  <div class="h-full flex flex-col overflow-hidden">
    <!-- Header -->
    <div class="shrink-0 px-4 h-12 flex items-center justify-between border-b border-gray-100">
      <div class="flex items-center gap-2">
        <button v-if="showClose" class="h-7 w-7 rounded-md flex items-center justify-center text-gray-400 hover:bg-gray-100" @click="$emit('close')">
          <Icon name="heroicons:x-mark" class="w-4 h-4" />
        </button>
        <span class="text-sm font-semibold text-gray-900">Instructions</span>
      </div>
      <button class="h-7 w-7 rounded-md flex items-center justify-center text-gray-400 hover:bg-gray-100" title="Refresh" @click="load">
        <Icon name="heroicons:arrow-path" :class="['w-4 h-4', { 'animate-spin': loading }]" />
      </button>
    </div>

    <div class="flex-1 min-h-0 flex">
      <!-- Tree -->
      <div class="w-1/2 max-w-[340px] min-w-[240px] border-r border-gray-100 overflow-y-auto py-1">
        <div v-if="loading && !allInstructions.length" class="flex items-center justify-center py-16 text-gray-400">
          <Icon name="heroicons:arrow-path" class="w-5 h-5 animate-spin" />
        </div>
        <div v-else-if="error" class="px-4 py-6 text-xs text-gray-500">{{ error }}</div>
        <template v-else>
          <div v-for="group in groups" :key="group.id" class="select-none">
            <!-- Group header -->
            <div class="group/row flex items-center gap-1.5 px-2 h-8 rounded-md mx-1 hover:bg-gray-50 cursor-pointer" @click="toggle(group.id)">
              <Icon :name="isOpen(group.id) ? 'heroicons:chevron-down' : 'heroicons:chevron-right'" class="w-3 h-3 text-gray-300 shrink-0" />
              <Icon v-if="group.isGlobal" name="heroicons:globe-alt" class="w-4 h-4 text-gray-500 shrink-0" />
              <DataSourceIcon v-else :type="group.type" class="h-4 shrink-0" />
              <span class="text-[13px] font-medium text-gray-800 truncate flex-1">{{ group.name }}</span>
              <span class="text-[11px] text-gray-400 tabular-nums">{{ group.instructions.length }}</span>
              <button v-if="group.canManage" class="shrink-0 w-5 h-5 rounded hover:bg-gray-200 text-gray-400 hover:text-gray-700 opacity-0 group-hover/row:opacity-100 flex items-center justify-center" title="Add instruction" @click.stop="startCreate(group)">
                <Icon name="heroicons:plus" class="w-3 h-3" />
              </button>
            </div>
            <!-- Instructions -->
            <template v-if="isOpen(group.id)">
              <p v-if="!group.instructions.length" class="text-[11px] text-gray-400 italic pl-9 py-1">No instructions yet.</p>
              <button
                v-for="inst in group.instructions"
                :key="group.id + ':' + inst.id"
                class="w-full flex items-start gap-1.5 pl-9 pr-2 py-1.5 text-left rounded-md mx-1 transition-colors"
                :class="selectedKey === group.id + ':' + inst.id ? 'bg-indigo-50' : 'hover:bg-gray-50'"
                @click="select(group, inst)"
              >
                <Icon name="heroicons:document-text" class="w-3.5 h-3.5 text-gray-300 mt-0.5 shrink-0" />
                <span class="text-[12px] leading-snug truncate" :class="selectedKey === group.id + ':' + inst.id ? 'text-indigo-700' : 'text-gray-700'">
                  {{ inst.title || (inst.text || '').slice(0, 60) || 'Untitled' }}
                </span>
              </button>
            </template>
          </div>
        </template>
      </div>

      <!-- Detail -->
      <div class="flex-1 min-w-0 flex flex-col overflow-hidden">
        <div v-if="!detail && !creating" class="flex-1 flex items-center justify-center px-6 text-center">
          <div>
            <Icon name="heroicons:document-text" class="w-8 h-8 text-gray-200 mx-auto" />
            <p class="mt-2 text-sm text-gray-400">Select an instruction to view or edit.</p>
          </div>
        </div>
        <template v-else>
          <!-- Detail header -->
          <div class="shrink-0 h-12 px-5 flex items-center justify-between border-b border-gray-100">
            <span class="text-xs font-medium text-gray-500">{{ creating ? 'New instruction' : (editing ? 'Editing' : 'Instruction') }}</span>
            <div class="flex items-center gap-1.5">
              <template v-if="!editing && !creating">
                <button v-if="canManageSelected" class="h-7 px-3 rounded-md border border-gray-200 text-gray-700 text-xs font-medium hover:bg-gray-50" @click="startEdit">Edit</button>
              </template>
              <template v-else>
                <button v-if="!creating && canManageSelected" class="h-7 px-3 rounded-md text-red-600 text-xs font-medium hover:bg-red-50 disabled:opacity-50" :disabled="deleting || saving" @click="remove">
                  <span class="inline-flex items-center gap-1"><Icon :name="deleting ? 'heroicons:arrow-path' : 'heroicons:trash'" :class="['w-3.5 h-3.5', { 'animate-spin': deleting }]" />{{ deleting ? 'Deleting…' : 'Delete' }}</span>
                </button>
                <span v-if="!creating && canManageSelected" class="w-px h-4 bg-gray-200 mx-0.5"></span>
                <button class="h-7 px-3 rounded-md text-gray-500 text-xs hover:bg-gray-100" @click="cancel">Cancel</button>
                <button class="h-7 px-3 rounded-md bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 disabled:opacity-50" :disabled="saving" @click="save">{{ saving ? 'Saving…' : (creating ? 'Create' : 'Save') }}</button>
              </template>
            </div>
          </div>
          <!-- Detail body -->
          <div class="flex-1 min-h-0 overflow-y-auto px-5 py-4">
            <input
              v-if="editing || creating"
              v-model="draft.title"
              type="text"
              placeholder="Title (optional)"
              class="w-full text-base font-semibold text-gray-900 outline-none placeholder:text-gray-300 mb-2"
            />
            <h3 v-else-if="detail?.title" class="text-base font-semibold text-gray-900 mb-2">{{ detail.title }}</h3>
            <div class="prose-instruction">
              <InstructionEditor
                :key="(creating ? 'new' : detail?.id) + (editing || creating ? '-edit' : '-view')"
                v-model="draft.text"
                mode="wysiwyg"
                :editable="editing || creating"
                :data-source-ids="draft.dataSourceIds"
                :is-all-data-sources="draft.dataSourceIds.length === 0"
                placeholder="Write the instruction in markdown… (type @ to mention a table or instruction)"
              />
            </div>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import InstructionEditor from '~/components/instructions/InstructionEditor.vue'
import { useCan } from '~/composables/usePermissions'

const props = defineProps<{
  agents: Array<{ id: string; name: string; type?: string; connections?: any[] }>
  showClose?: boolean
}>()
defineEmits<{ (e: 'close'): void }>()

const toast = useToast()
const allInstructions = ref<any[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const openGroups = ref<Set<string>>(new Set())
const GLOBAL_ID = '__global__'

const isOpen = (id: string) => openGroups.value.has(id)
const toggle = (id: string) => { const s = new Set(openGroups.value); s.has(id) ? s.delete(id) : s.add(id); openGroups.value = s }

const canManageAgent = (agentId: string) =>
  useCan('manage_instructions') || useCan('manage_instructions', { type: 'data_source', id: agentId })
const canManageGlobal = () => useCan('manage_instructions')

// One fetch, grouped client-side: each selected agent + a Global group (no data source).
const groups = computed(() => {
  const out = (props.agents || []).map((a) => ({
    id: a.id,
    name: a.name,
    type: a.type || a.connections?.[0]?.type,
    isGlobal: false,
    canManage: canManageAgent(a.id),
    instructions: allInstructions.value.filter((i) => (i.data_sources || []).some((ds: any) => ds.id === a.id)),
  }))
  out.push({
    id: GLOBAL_ID,
    name: 'Global',
    type: undefined,
    isGlobal: true,
    canManage: canManageGlobal(),
    instructions: allInstructions.value.filter((i) => !(i.data_sources?.length)),
  })
  return out
})

async function load() {
  loading.value = true
  error.value = null
  try {
    const { data, error: err } = await useMyFetch<any>('/api/instructions', {
      method: 'GET',
      query: { include_own: true, include_drafts: true, limit: 200 },
    })
    if (err.value) throw new Error('Failed to load instructions')
    const payload: any = data.value
    allInstructions.value = payload?.items || payload || []
    // Open all groups by default the first time.
    if (openGroups.value.size === 0) openGroups.value = new Set(groups.value.map((g) => g.id))
  } catch (e: any) {
    error.value = e?.message || 'Failed to load instructions'
  } finally {
    loading.value = false
  }
}

// ── Detail / edit / create ────────────────────────────────────────────────────
const detail = ref<any | null>(null)
const selectedKey = ref<string | null>(null)
const editing = ref(false)
const creating = ref(false)
const saving = ref(false)
const deleting = ref(false)
const createScope = ref<{ agentId: string | null }>({ agentId: null })
const draft = reactive<{ title: string; text: string; dataSourceIds: string[] }>({ title: '', text: '', dataSourceIds: [] })

const canManageSelected = computed(() => {
  if (creating.value) return true
  const dsIds = (detail.value?.data_sources || []).map((d: any) => d.id)
  if (!dsIds.length) return canManageGlobal()
  return useCan('manage_instructions') || dsIds.some((id: string) => useCan('manage_instructions', { type: 'data_source', id }))
})

function select(group: any, inst: any) {
  creating.value = false
  editing.value = false
  detail.value = inst
  selectedKey.value = group.id + ':' + inst.id
  syncDraft(inst)
}
function syncDraft(inst: any) {
  draft.title = inst.title || ''
  draft.text = inst.text || ''
  draft.dataSourceIds = (inst.data_sources || []).map((d: any) => d.id)
}
function startEdit() { if (detail.value) { syncDraft(detail.value); editing.value = true } }
function startCreate(group: any) {
  detail.value = null
  selectedKey.value = null
  editing.value = false
  creating.value = true
  createScope.value = { agentId: group.isGlobal ? null : group.id }
  draft.title = ''
  draft.text = ''
  draft.dataSourceIds = group.isGlobal ? [] : [group.id]
}
function cancel() {
  if (creating.value) { creating.value = false; detail.value = null; selectedKey.value = null }
  else { editing.value = false; if (detail.value) syncDraft(detail.value) }
}

async function save() {
  if (!draft.text.trim()) { toast.add({ title: 'Instruction text is required', color: 'red' }); return }
  saving.value = true
  try {
    const body: any = {
      title: draft.title || null, text: draft.text, kind: 'instruction',
      load_mode: 'always', status: 'published', category: 'general',
      data_source_ids: draft.dataSourceIds, label_ids: [], references: [],
    }
    if (creating.value) {
      const endpoint = draft.dataSourceIds.length ? '/api/instructions' : '/api/instructions/global'
      const { data, error: err } = await useMyFetch<any>(endpoint, { method: 'POST', body })
      if (err.value) throw new Error((err.value as any)?.data?.detail || 'Create failed')
      toast.add({ title: 'Created', color: 'green' })
      creating.value = false
      const row = data.value
      if (row?.id) { allInstructions.value = [...allInstructions.value, row]; select(groups.value.find(g => createScope.value.agentId ? g.id === createScope.value.agentId : g.isGlobal) || groups.value[0], row) }
      else await load()
    } else if (detail.value) {
      const { data, error: err } = await useMyFetch<any>(`/api/instructions/${detail.value.id}`, { method: 'PUT', body })
      if (err.value) throw new Error((err.value as any)?.data?.detail || 'Save failed')
      toast.add({ title: 'Saved', color: 'green' })
      editing.value = false
      const row = data.value
      if (row) {
        const idx = allInstructions.value.findIndex((i) => i.id === row.id)
        if (idx >= 0) { allInstructions.value[idx] = { ...allInstructions.value[idx], ...row }; allInstructions.value = [...allInstructions.value] }
        detail.value = { ...detail.value, ...row }; syncDraft(detail.value)
      }
    }
  } catch (e: any) {
    toast.add({ title: 'Error', description: e?.message, color: 'red' })
  } finally { saving.value = false }
}

async function remove() {
  if (!detail.value) return
  const label = detail.value.title || 'this instruction'
  if (!window.confirm(`Delete "${label}"? This can't be undone.`)) return
  deleting.value = true
  try {
    const id = detail.value.id
    const { error: err } = await useMyFetch(`/api/instructions/${id}`, { method: 'DELETE' })
    if (err.value) throw new Error((err.value as any)?.data?.detail || 'Delete failed')
    toast.add({ title: 'Deleted', color: 'green' })
    allInstructions.value = allInstructions.value.filter((i) => i.id !== id)
    detail.value = null; selectedKey.value = null; editing.value = false
  } catch (e: any) {
    toast.add({ title: 'Error', description: e?.message, color: 'red' })
  } finally { deleting.value = false }
}

onMounted(load)
</script>
