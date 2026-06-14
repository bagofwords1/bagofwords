<template>
  <div class="flex flex-col h-[calc(100vh-64px)] text-sm">
    <!-- Header -->
    <div class="flex items-center justify-between pl-3 pr-4 py-3 shrink-0">
      <div>
        <h1 class="text-[15px] font-semibold text-gray-900 tracking-tight">Knowledge</h1>
        <p class="text-xs text-gray-400 mt-0.5">The instructions, rules and skills your agents reason with.</p>
      </div>
      <div class="flex items-center gap-2">
        <button
          v-if="pendingCount > 0"
          class="inline-flex items-center gap-1.5 h-8 px-3 rounded-md border border-amber-200 bg-amber-50 text-amber-700 text-xs font-medium hover:bg-amber-100 transition-colors"
          @click="expand('pending', true)"
        >
          <span class="w-1.5 h-1.5 rounded-full bg-amber-500"></span>
          {{ pendingCount }} pending review
        </button>
        <button
          class="inline-flex items-center gap-1.5 h-8 px-3 rounded-md bg-gray-900 text-white text-xs font-medium hover:bg-black transition-colors"
          @click="openCreate"
        >
          <UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" />
          Add instruction
        </button>
      </div>
    </div>

    <!-- Body: tree → detail → versions -->
    <div class="flex-1 min-h-0 flex border-t border-gray-200">
      <!-- ── Pane 1: Tree ───────────────────────────────── -->
      <aside class="w-[300px] shrink-0 border-r border-gray-200 flex flex-col">
        <div class="px-2 pt-2.5 pb-2 space-y-2">
          <div class="relative">
            <UIcon name="i-heroicons-magnifying-glass" class="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400" />
            <input
              v-model="search"
              type="text"
              placeholder="Search everything…"
              class="w-full h-8 pl-8 pr-2 text-xs bg-gray-50 border border-gray-200 rounded-md outline-none focus:border-gray-400 focus:bg-white placeholder:text-gray-400"
            />
          </div>
          <div class="flex items-center gap-1">
            <button
              v-for="f in statusFilters" :key="f.value"
              class="h-6 px-2 rounded text-[11px] font-medium transition-colors"
              :class="statusFilter === f.value ? 'bg-gray-900 text-white' : 'text-gray-500 hover:bg-gray-100'"
              @click="statusFilter = f.value as any"
            >{{ f.label }}</button>
          </div>
        </div>

        <div class="flex-1 overflow-y-auto px-2 pb-2 space-y-0.5">
          <TreeGroup label="All instructions" icon="i-heroicons-square-3-stack-3d" :count="allInstructions.length" :open="isOpen('all')" @toggle="expand('all')">
            <InstrLeaf v-for="ins in listFor('all')" :key="ins.id" :ins="ins" />
          </TreeGroup>
          <TreeGroup label="Skills" icon="i-heroicons-sparkles" :count="skillCount" :open="isOpen('skills')" @toggle="expand('skills')">
            <div v-if="skillCount === 0" class="pl-8 py-1 text-[11px] text-gray-300 italic">No skills yet.</div>
            <InstrLeaf v-for="ins in listFor('skills')" :key="ins.id" :ins="ins" />
          </TreeGroup>
          <TreeGroup label="Pending review" icon="i-heroicons-clock" :count="pendingCount" :count-accent="pendingCount > 0" :open="isOpen('pending')" @toggle="expand('pending')">
            <div v-if="pendingCount === 0" class="pl-8 py-1 text-[11px] text-gray-300 italic">Nothing awaiting review.</div>
            <InstrLeaf v-for="ins in listFor('pending')" :key="ins.id" :ins="ins" />
          </TreeGroup>

          <div class="h-px bg-gray-100 my-2 mx-1"></div>

          <TreeGroup label="Global / Org-wide" icon="i-heroicons-globe-alt" :count="globalCount" :open="isOpen('global')" @toggle="expand('global')">
            <InstrLeaf v-for="ins in listFor('global')" :key="ins.id" :ins="ins" />
          </TreeGroup>

          <div class="px-2 pt-3 pb-1 text-[10px] font-semibold uppercase tracking-wider text-gray-400">Agents</div>

          <template v-for="agent in agents" :key="agent.id">
            <TreeGroup :label="agent.name" :count="agentCount(agent.id)" :pending="agentPending(agent.id)" :open="isOpen('agent:' + agent.id)" @toggle="expand('agent:' + agent.id)">
              <template #icon>
                <DataSourceIcon :type="agent.type" class="w-4 h-4 shrink-0 grayscale opacity-80" />
              </template>

              <!-- Tables -->
              <TreeGroup label="Tables" icon="i-heroicons-table-cells" :count="agentTables[agent.id]?.length" :indent="1" :open="isOpen('tables:' + agent.id)" @toggle="expand('tables:' + agent.id)">
                <TreeGroup
                  v-for="t in (agentTables[agent.id] || [])" :key="t.id"
                  :label="t.name" :icon="t.is_active ? 'i-heroicons-check-circle' : 'i-heroicons-table-cells'"
                  :count="listForTable(agent.id, t.id).length || undefined" mono :indent="2"
                  :open="isOpen('table:' + agent.id + ':' + t.id)" @toggle="expand('table:' + agent.id + ':' + t.id)"
                >
                  <InstrLeaf v-for="ins in listForTable(agent.id, t.id)" :key="ins.id" :ins="ins" :indent="3" />
                  <div v-if="listForTable(agent.id, t.id).length === 0" class="py-1 text-[11px] text-gray-300 italic" style="padding-left:62px">No rules attached.</div>
                </TreeGroup>
                <div v-if="(agentTables[agent.id]?.length ?? -1) === 0" class="py-1 text-[11px] text-gray-300 italic" style="padding-left:48px">No accessible tables.</div>
              </TreeGroup>

              <!-- Tools -->
              <TreeGroup label="Tools" icon="i-heroicons-wrench-screwdriver" :count="agentTools[agent.id]?.length" :indent="1" :open="isOpen('tools:' + agent.id)" @toggle="expand('tools:' + agent.id)">
                <div v-for="tool in (agentTools[agent.id] || [])" :key="tool.id || tool.name"
                     class="flex items-center gap-2 h-7 rounded-md text-xs text-gray-600" style="padding-left:48px;padding-right:8px">
                  <UIcon name="i-heroicons-wrench-screwdriver" class="w-3 h-3 text-gray-300 shrink-0" />
                  <span class="flex-1 text-left truncate font-mono text-[11px]">{{ tool.name }}</span>
                  <span v-if="tool.is_enabled === false" class="text-[9px] px-1 rounded bg-gray-100 text-gray-400">off</span>
                  <span v-else-if="tool.policy && tool.policy !== 'allow'" class="text-[9px] px-1 rounded bg-gray-100 text-gray-500">{{ tool.policy }}</span>
                </div>
                <div v-if="(agentTools[agent.id]?.length ?? -1) === 0" class="py-1 text-[11px] text-gray-300 italic" style="padding-left:48px">No tools connected.</div>
              </TreeGroup>

              <!-- Instructions -->
              <TreeGroup label="Instructions" icon="i-heroicons-document-text" :count="listForAgent(agent.id).length" :indent="1" :open="isOpen('instr:' + agent.id)" @toggle="expand('instr:' + agent.id)">
                <InstrLeaf v-for="ins in listForAgent(agent.id)" :key="ins.id" :ins="ins" :indent="2" />
                <div v-if="listForAgent(agent.id).length === 0" class="py-1 text-[11px] text-gray-300 italic" style="padding-left:48px">No instructions yet.</div>
              </TreeGroup>
            </TreeGroup>
          </template>
        </div>

        <!-- Bulk bar -->
        <div v-if="selectedIds.size > 0" class="px-3 py-2 border-t border-gray-200 flex items-center gap-2 text-xs">
          <span class="text-gray-600 font-medium">{{ selectedIds.size }} selected</span>
          <div class="flex-1"></div>
          <button class="h-6 px-2 rounded hover:bg-gray-100 text-gray-600" @click="bulk({ status: 'published' })">Activate</button>
          <button class="h-6 px-2 rounded hover:bg-gray-100 text-gray-600" @click="bulk({ status: 'draft' })">Deactivate</button>
          <button class="h-6 px-2 rounded hover:bg-red-50 text-red-500" @click="bulkDelete">Delete</button>
          <button class="h-6 w-6 rounded hover:bg-gray-100 text-gray-400 flex items-center justify-center" @click="clearSelection">
            <UIcon name="i-heroicons-x-mark" class="w-3.5 h-3.5" />
          </button>
        </div>
      </aside>

      <!-- ── Pane 2: Detail ───────────────────────────── -->
      <section class="flex-1 min-w-0 flex flex-col">
        <template v-if="detail">
          <div class="h-11 shrink-0 px-4 flex items-center justify-between border-b border-gray-100">
            <div class="flex items-center gap-2 min-w-0">
              <span class="w-1.5 h-1.5 rounded-full" :class="h.getStatusIconClass(detail)"></span>
              <span class="text-xs font-medium text-gray-500">{{ h.getStatusLabel(detail) }}</span>
            </div>
            <div class="flex items-center gap-1.5">
              <button class="h-7 w-7 rounded-md flex items-center justify-center transition-colors" :class="showHistory ? 'bg-gray-100 text-gray-700' : 'text-gray-400 hover:bg-gray-100'" title="Version history" @click="showHistory = !showHistory">
                <UIcon name="i-heroicons-clock" class="w-4 h-4" />
              </button>
              <template v-if="!editing">
                <button class="h-7 px-3 rounded-md border border-gray-200 text-gray-700 text-xs font-medium hover:bg-gray-50" @click="startEdit">Edit</button>
              </template>
              <template v-else>
                <button class="h-7 px-3 rounded-md text-gray-500 text-xs hover:bg-gray-100" @click="cancelEdit">Cancel</button>
                <button class="h-7 px-3 rounded-md bg-gray-900 text-white text-xs font-medium hover:bg-black disabled:opacity-50" :disabled="saving" @click="save">{{ saving ? 'Saving…' : 'Save' }}</button>
              </template>
            </div>
          </div>

          <div class="flex-1 overflow-y-auto px-8 py-6 max-w-3xl">
            <input v-if="editing" v-model="draft.title" placeholder="Untitled instruction" class="w-full text-lg font-semibold text-gray-900 outline-none placeholder:text-gray-300 mb-3" />
            <h2 v-else class="text-lg font-semibold text-gray-900 mb-3">{{ displayTitle(detail) }}</h2>

            <div class="flex flex-wrap items-center gap-2 mb-5">
              <USelectMenu v-if="editing" v-model="draft.load_mode" :options="loadModeOptions" value-attribute="value" option-attribute="label" size="xs" />
              <span v-else class="inline-flex items-center px-2 h-6 rounded-md bg-gray-100 text-gray-600 text-[11px] font-medium">
                <UIcon name="i-heroicons-bolt" class="w-3 h-3 mr-1 text-gray-400" />{{ h.getLoadModeLabel(detail.load_mode) }}
              </span>
              <USelectMenu v-if="editing" v-model="draft.status" :options="statusOptions" value-attribute="value" option-attribute="label" size="xs" />
              <span class="inline-flex items-center px-2 h-6 rounded-md bg-gray-100 text-gray-600 text-[11px] font-medium">
                <UIcon :name="h.getSourceIcon(detail)" class="w-3 h-3 mr-1 text-gray-400" />{{ h.getSourceTooltip(detail) }}
              </span>
            </div>

            <div class="prose-instruction">
              <InstructionEditor :key="detail.id + (editing ? '-edit' : '-view')" v-model="draft.text" mode="wysiwyg" :editable="editing"
                :data-source-ids="(detail.data_sources || []).map(d => d.id)" :is-all-data-sources="(detail.data_sources || []).length === 0"
                placeholder="Write the instruction in markdown…" />
            </div>

            <div class="mt-6 pt-5 border-t border-gray-100">
              <div class="text-[10px] font-semibold uppercase tracking-wider text-gray-400 mb-2">Used by</div>
              <div v-if="(detail.data_sources || []).length === 0" class="flex items-center gap-1.5 text-xs text-gray-500">
                <UIcon name="i-heroicons-globe-alt" class="w-3.5 h-3.5 text-gray-400" /> All agents (global)
              </div>
              <div v-else class="flex flex-wrap gap-1.5">
                <span v-for="ds in detail.data_sources" :key="ds.id" class="inline-flex items-center gap-1.5 px-2 h-6 rounded-md bg-gray-100 text-gray-600 text-[11px]">
                  <DataSourceIcon :type="ds.type" class="w-3 h-3 grayscale opacity-80" /> {{ ds.name }}
                </span>
              </div>
              <template v-if="(detail.references || []).length">
                <div class="text-[10px] font-semibold uppercase tracking-wider text-gray-400 mt-4 mb-2">Attached to</div>
                <div class="flex flex-wrap gap-1.5">
                  <span v-for="(ref, i) in detail.references" :key="i" class="inline-flex items-center gap-1 px-2 h-6 rounded-md bg-gray-100 text-gray-600 text-[11px] font-mono">
                    <UIcon :name="h.getRefIcon(ref.object_type)" class="w-3 h-3 text-gray-400" /> {{ refLabel(ref) }}
                  </span>
                </div>
              </template>
            </div>
          </div>
        </template>

        <div v-else class="flex-1 flex flex-col items-center justify-center text-center px-6">
          <UIcon name="i-heroicons-document-text" class="w-9 h-9 text-gray-200" />
          <p class="text-sm text-gray-400 mt-3">Open an agent and pick an instruction to view and edit it.</p>
        </div>
      </section>

      <!-- ── Pane 3: Versions ─────────────────────────── -->
      <aside v-if="detail && showHistory" class="w-60 shrink-0 border-l border-gray-200 flex flex-col">
        <div class="h-11 shrink-0 px-3 flex items-center border-b border-gray-100">
          <span class="text-[10px] font-semibold uppercase tracking-wider text-gray-400">Version history</span>
        </div>
        <div class="flex-1 overflow-y-auto p-2 space-y-0.5">
          <div v-if="versionsLoading" class="p-3 text-center text-[11px] text-gray-400">Loading…</div>
          <div v-else-if="versions.length === 0" class="p-3 text-center text-[11px] text-gray-300">No history yet.</div>
          <div v-for="(v, i) in versions" :key="v.id" class="px-2.5 py-2 rounded-md hover:bg-gray-50 transition-colors">
            <div class="flex items-center justify-between">
              <span class="text-xs font-medium text-gray-700">v{{ v.version_number }}</span>
              <span v-if="i === 0" class="text-[9px] font-semibold uppercase tracking-wider text-gray-500 bg-gray-100 px-1.5 rounded">current</span>
              <button v-else class="text-[10px] text-gray-400 hover:text-gray-700" @click="restore(v)">Restore</button>
            </div>
            <div class="text-[10px] text-gray-400 mt-0.5">{{ fmtDate(v.created_at) }}</div>
          </div>
        </div>
      </aside>
    </div>

    <InstructionModalComponent v-model="showCreate" :instruction="null" @instruction-saved="onCreated" />
  </div>
</template>

<script setup lang="ts">
import { h as createElement } from 'vue'
import InstructionEditor from '~/components/instructions/InstructionEditor.vue'
import InstructionModalComponent from '~/components/InstructionModalComponent.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import { useInstructionHelpers, type Instruction } from '~/composables/useInstructionHelpers'

const h = useInstructionHelpers()
const toast = useToast()

// ── State ───────────────────────────────────────────────
const allInstructions = ref<Instruction[]>([])
const agents = ref<{ id: string; name: string; type?: string }[]>([])
const search = ref('')
const statusFilter = ref<'all' | 'published' | 'draft'>('all')

const expanded = ref<Set<string>>(new Set())
const agentTables = ref<Record<string, { id: string; name: string; is_active: boolean }[]>>({})
const agentTools = ref<Record<string, any[]>>({})
const agentLoaded = ref<Set<string>>(new Set())

const selectedId = ref<string | null>(null)
const detail = ref<Instruction | null>(null)
const editing = ref(false)
const saving = ref(false)
const draft = reactive<{ title: string; text: string; load_mode: string; status: string }>({ title: '', text: '', load_mode: 'always', status: 'published' })

const showHistory = ref(true)
const versions = ref<any[]>([])
const versionsLoading = ref(false)

const selectedIds = ref<Set<string>>(new Set())
const showCreate = ref(false)

const statusFilters = [
  { value: 'all', label: 'All' },
  { value: 'published', label: 'Active' },
  { value: 'draft', label: 'Inactive' },
]
const loadModeOptions = [
  { value: 'always', label: 'Always' },
  { value: 'intelligent', label: 'Smart' },
  { value: 'disabled', label: 'Off' },
]
const statusOptions = [
  { value: 'published', label: 'Active' },
  { value: 'draft', label: 'Inactive' },
]

// ── Expansion ───────────────────────────────────────────
const isOpen = (key: string) => expanded.value.has(key)
const expand = (key: string, force?: boolean) => {
  if (force) expanded.value.add(key)
  else if (expanded.value.has(key)) expanded.value.delete(key)
  else expanded.value.add(key)
  if (key.startsWith('agent:') && expanded.value.has(key)) {
    const id = key.slice('agent:'.length)
    expanded.value.add('instr:' + id) // show instructions immediately
    loadAgentMeta(id)
  }
  expanded.value = new Set(expanded.value)
}

// ── Fetching ────────────────────────────────────────────
const fetchAll = async () => {
  try {
    const { data } = await useMyFetch<any>('/api/instructions', {
      method: 'GET',
      query: { skip: 0, limit: 200, include_own: true, include_drafts: true, include_archived: true },
    })
    allInstructions.value = data.value?.items || []
  } catch (e) { console.error(e) }
}
const fetchAgents = async () => {
  try {
    const { data } = await useMyFetch<any[]>('/data_sources/active', { method: 'GET' })
    agents.value = (data.value || []).map((d: any) => ({ id: d.id, name: d.name, type: d.type }))
  } catch (e) { console.error(e) }
}
const loadAgentMeta = async (id: string) => {
  if (agentLoaded.value.has(id)) return
  agentLoaded.value.add(id)
  // Tables — use the same routes as the agent schema page (overlay-aware).
  try {
    const { data } = await useMyFetch<any>(`/data_sources/${id}/full_schema`, { method: 'GET' })
    const items = Array.isArray(data.value) ? data.value : (data.value?.items || [])
    agentTables.value[id] = items.map((t: any) => ({ id: String(t.id ?? t.name), name: t.name, is_active: t.is_active !== false }))
  } catch { agentTables.value[id] = [] }
  // Tools — per-agent effective tools (with overlay).
  try {
    const { data } = await useMyFetch<any[]>(`/data_sources/${id}/tools`, { method: 'GET' })
    agentTools.value[id] = data.value || []
  } catch { agentTools.value[id] = [] }
  agentTables.value = { ...agentTables.value }
  agentTools.value = { ...agentTools.value }
}

// ── Counts ──────────────────────────────────────────────
const isPending = (ins: Instruction) => h.getEffectiveStatus(ins) === 'pending_review'
const pendingCount = computed(() => allInstructions.value.filter(isPending).length)
const globalCount = computed(() => allInstructions.value.filter(i => (i.data_sources || []).length === 0).length)
const skillCount = computed(() => allInstructions.value.filter(i => (i as any).kind === 'skill').length)
const agentCount = (id: string) => allInstructions.value.filter(i => (i.data_sources || []).some(d => d.id === id)).length
const agentPending = (id: string) => allInstructions.value.some(i => isPending(i) && (i.data_sources || []).some(d => d.id === id))

// ── Leaf lists (search + status) ────────────────────────
const applyFilters = (list: Instruction[]) => {
  let out = list
  if (statusFilter.value !== 'all') out = out.filter(i => i.status === statusFilter.value && !isPending(i))
  const q = search.value.trim().toLowerCase()
  if (q) out = out.filter(i => (i.title || '').toLowerCase().includes(q) || (i.text || '').toLowerCase().includes(q))
  return out
}
const listFor = (kind: string) => {
  let base = allInstructions.value
  if (kind === 'skills') base = base.filter(i => (i as any).kind === 'skill')
  else if (kind === 'pending') base = base.filter(isPending)
  else if (kind === 'global') base = base.filter(i => (i.data_sources || []).length === 0)
  return applyFilters(base)
}
const hasTableRef = (ins: Instruction) => (ins.references || []).some((r: any) => r.object_type === 'datasource_table')
const listForAgent = (id: string) => applyFilters(allInstructions.value.filter(i => (i.data_sources || []).some(d => d.id === id) && !hasTableRef(i)))
const listForTable = (agentId: string, tableId: string) => applyFilters(allInstructions.value.filter(i =>
  (i.data_sources || []).some(d => d.id === agentId) &&
  (i.references || []).some((r: any) => r.object_type === 'datasource_table' && String(r.object_id) === tableId)
))

// ── Detail ──────────────────────────────────────────────
const openInstruction = async (ins: Instruction) => {
  selectedId.value = ins.id; detail.value = ins; editing.value = false
  syncDraft(ins); loadVersions(ins.id)
  try {
    const { data } = await useMyFetch<Instruction>(`/api/instructions/${ins.id}`, { method: 'GET' })
    if (data.value && selectedId.value === ins.id) { detail.value = data.value; if (!editing.value) syncDraft(data.value) }
  } catch (e) { /* keep list copy */ }
}
const syncDraft = (ins: Instruction) => {
  draft.title = ins.title || ''; draft.text = ins.text || ''
  draft.load_mode = ins.load_mode || 'always'; draft.status = ins.status || 'published'
}
const startEdit = () => { if (detail.value) { syncDraft(detail.value); editing.value = true } }
const cancelEdit = () => { if (detail.value) syncDraft(detail.value); editing.value = false }
const save = async () => {
  if (!detail.value) return
  saving.value = true
  try {
    const { data, error } = await useMyFetch<Instruction>(`/api/instructions/${detail.value.id}`, {
      method: 'PUT', body: { title: draft.title || null, text: draft.text, load_mode: draft.load_mode, status: draft.status },
    })
    if (error.value) throw new Error((error.value as any)?.message || 'Save failed')
    toast.add({ title: 'Saved', color: 'green' }); editing.value = false
    if (data.value) detail.value = { ...detail.value, ...data.value }
    await fetchAll()
    const fresh = allInstructions.value.find(i => i.id === detail.value?.id)
    if (fresh) { detail.value = fresh; syncDraft(fresh) }
    loadVersions(detail.value!.id)
  } catch (e: any) { toast.add({ title: 'Error', description: e.message, color: 'red' }) } finally { saving.value = false }
}

// ── Versions ────────────────────────────────────────────
const loadVersions = async (id: string) => {
  versionsLoading.value = true; versions.value = []
  try {
    const { data } = await useMyFetch<any>(`/api/instructions/${id}/versions`, { method: 'GET', query: { limit: 50 } })
    versions.value = data.value?.items || []
  } catch (e) { /* none */ } finally { versionsLoading.value = false }
}
const restore = async (v: any) => {
  if (!detail.value) return
  if (!window.confirm(`Restore version v${v.version_number}? This creates a new version.`)) return
  try {
    await useMyFetch(`/api/instructions/${detail.value.id}/versions/${v.id}/revert`, { method: 'POST' })
    toast.add({ title: `Restored v${v.version_number}`, color: 'green' })
    await fetchAll()
    const fresh = allInstructions.value.find(i => i.id === detail.value?.id)
    if (fresh) openInstruction(fresh)
  } catch (e: any) { toast.add({ title: 'Error', description: e?.message, color: 'red' }) }
}

// ── Multi-select / bulk ─────────────────────────────────
const toggleSelect = (id: string) => {
  if (selectedIds.value.has(id)) selectedIds.value.delete(id); else selectedIds.value.add(id)
  selectedIds.value = new Set(selectedIds.value)
}
const clearSelection = () => { selectedIds.value = new Set() }
const bulk = async (updates: Record<string, any>) => {
  const ids = Array.from(selectedIds.value); if (!ids.length) return
  try {
    await useMyFetch('/api/instructions/bulk', { method: 'PUT', body: { ids, ...updates } })
    toast.add({ title: `Updated ${ids.length}`, color: 'green' }); clearSelection(); await fetchAll()
  } catch (e: any) { toast.add({ title: 'Error', description: e?.message, color: 'red' }) }
}
const bulkDelete = async () => {
  const ids = Array.from(selectedIds.value); if (!ids.length) return
  if (!window.confirm(`Delete ${ids.length} instruction(s)?`)) return
  try {
    await useMyFetch('/api/instructions/bulk', { method: 'DELETE', body: { ids } })
    toast.add({ title: `Deleted ${ids.length}`, color: 'green' })
    if (selectedId.value && ids.includes(selectedId.value)) { detail.value = null; selectedId.value = null }
    clearSelection(); await fetchAll()
  } catch (e: any) { toast.add({ title: 'Error', description: e?.message, color: 'red' }) }
}

// ── Create ──────────────────────────────────────────────
const openCreate = () => { showCreate.value = true }
const onCreated = async () => { showCreate.value = false; await fetchAll() }

// ── Display helpers ─────────────────────────────────────
const displayTitle = (ins: Instruction) => ins.title || (ins.text || '').split('\n')[0].slice(0, 60) || 'Untitled'
const refLabel = (ref: any) => ref.display_text || ref.object?.name || ref.object_type
const fmtDate = (s?: string) => { if (!s) return ''; try { return new Date(s).toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) } catch { return s } }

// ── Inline tree sub-components ──────────────────────────
const TreeGroup = defineComponent({
  props: {
    label: String, icon: String, count: { type: Number, default: undefined },
    countAccent: Boolean, pending: Boolean, open: Boolean, mono: Boolean, indent: { type: Number, default: 0 },
  },
  emits: ['toggle'],
  setup(props, { slots, emit }) {
    return () => createElement('div', {}, [
      createElement('button', {
        class: ['w-full flex items-center gap-1.5 h-7 rounded-md text-xs text-gray-600 hover:bg-gray-100 transition-colors min-w-0'],
        style: { paddingLeft: (6 + props.indent * 14) + 'px', paddingRight: '8px' },
        onClick: () => emit('toggle'),
      }, [
        createElement(resolveComponent('UIcon'), { name: 'i-heroicons-chevron-right', class: ['w-3 h-3 text-gray-300 transition-transform shrink-0', props.open ? 'rotate-90' : ''] }),
        slots.icon ? slots.icon() : (props.icon ? createElement(resolveComponent('UIcon'), { name: props.icon, class: 'w-4 h-4 text-gray-400 shrink-0' }) : null),
        createElement('span', { class: ['flex-1 text-left truncate', props.mono ? 'font-mono text-[11px]' : ''] }, props.label),
        props.pending ? createElement('span', { class: 'w-1.5 h-1.5 rounded-full bg-amber-400 shrink-0', title: 'Has pending changes' }) : null,
        props.count !== undefined ? createElement('span', { class: ['text-[11px] tabular-nums shrink-0', props.countAccent ? 'text-amber-600 font-medium' : 'text-gray-400'] }, String(props.count)) : null,
      ]),
      props.open ? createElement('div', { class: 'space-y-0.5 mt-0.5' }, slots.default ? slots.default() : []) : null,
    ])
  },
})

const InstrLeaf = defineComponent({
  props: { ins: { type: Object as () => Instruction, required: true }, indent: { type: Number, default: 0 } },
  setup(props) {
    return () => {
      const ins = props.ins
      const sel = selectedId.value === ins.id
      const checked = selectedIds.value.has(ins.id)
      return createElement('button', {
        class: ['group w-full flex items-center gap-2 h-7 rounded-md text-xs transition-colors min-w-0', sel ? 'bg-gray-100 text-gray-900' : 'text-gray-600 hover:bg-gray-100'],
        style: { paddingLeft: (20 + props.indent * 14) + 'px', paddingRight: '8px' },
        onClick: () => openInstruction(ins),
      }, [
        createElement('span', {
          class: ['shrink-0 w-3.5 h-3.5 rounded border flex items-center justify-center transition-colors', checked ? 'bg-gray-900 border-gray-900' : 'border-gray-300 opacity-0 group-hover:opacity-100'],
          onClick: (e: Event) => { e.stopPropagation(); toggleSelect(ins.id) },
        }, checked ? [createElement(resolveComponent('UIcon'), { name: 'i-heroicons-check', class: 'w-2.5 h-2.5 text-white' })] : []),
        createElement('span', { class: ['shrink-0 w-1.5 h-1.5 rounded-full', h.getStatusIconClass(ins)], title: h.getStatusTooltip(ins) }),
        createElement('span', { class: 'flex-1 text-left truncate' }, displayTitle(ins)),
        createElement(resolveComponent('UIcon'), { name: h.getSourceIcon(ins), class: 'w-3 h-3 text-gray-300 shrink-0', title: h.getSourceTooltip(ins) }),
        createElement('span', { class: 'shrink-0 inline-flex items-center px-1.5 h-4 rounded bg-gray-100 text-gray-500 text-[10px] font-medium' }, h.getLoadModeLabel(ins.load_mode)),
        (ins.data_sources && ins.data_sources.length > 1)
          ? createElement('span', { class: 'shrink-0 inline-flex items-center px-1 h-4 rounded bg-gray-100 text-gray-500 text-[10px] font-medium', title: ins.data_sources.map(d => d.name).join(', ') }, String(ins.data_sources.length))
          : null,
      ])
    }
  },
})

onMounted(async () => { await Promise.all([fetchAgents(), fetchAll()]) })
</script>

<style scoped>
.prose-instruction :deep(.tiptap-prose) { min-height: 80px; }
</style>
