<template>
  <div class="flex flex-col h-[calc(100vh-90px)] text-sm">
    <!-- Header -->
    <div class="flex items-center justify-between px-1 pb-3 shrink-0">
      <div>
        <h1 class="text-[15px] font-semibold text-gray-900 tracking-tight">Knowledge</h1>
        <p class="text-xs text-gray-400 mt-0.5">The instructions, rules and skills your agents reason with.</p>
      </div>
      <div class="flex items-center gap-2">
        <button
          v-if="pendingCount > 0"
          class="inline-flex items-center gap-1.5 h-8 px-3 rounded-md border border-amber-200 bg-amber-50 text-amber-700 text-xs font-medium hover:bg-amber-100 transition-colors"
          @click="selectNode({ kind: 'pending' })"
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

    <!-- Body: panes -->
    <div class="flex-1 min-h-0 flex border border-gray-200 rounded-xl overflow-hidden bg-white">
      <!-- ── Pane 1: Tree ─────────────────────────────── -->
      <aside class="w-60 shrink-0 border-r border-gray-200 flex flex-col bg-gray-50/40">
        <!-- Search -->
        <div class="p-2.5 border-b border-gray-200/70">
          <div class="relative">
            <UIcon name="i-heroicons-magnifying-glass" class="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400" />
            <input
              v-model="search"
              type="text"
              placeholder="Search everything…"
              class="w-full h-8 pl-8 pr-2 text-xs bg-white border border-gray-200 rounded-md outline-none focus:border-gray-400 placeholder:text-gray-400"
            />
          </div>
        </div>

        <!-- Tree body -->
        <div class="flex-1 overflow-y-auto p-2 space-y-0.5">
          <!-- Smart views -->
          <button :class="nodeClass({ kind: 'all' })" @click="selectNode({ kind: 'all' })">
            <UIcon name="i-heroicons-square-3-stack-3d" class="w-4 h-4 text-gray-400" />
            <span class="flex-1 text-left truncate">All instructions</span>
            <span class="text-[11px] text-gray-400 tabular-nums">{{ allInstructions.length }}</span>
          </button>
          <button :class="nodeClass({ kind: 'skills' })" @click="selectNode({ kind: 'skills' })">
            <UIcon name="i-heroicons-sparkles" class="w-4 h-4 text-gray-400" />
            <span class="flex-1 text-left truncate">Skills</span>
            <span class="text-[11px] text-gray-400 tabular-nums">{{ skillCount }}</span>
          </button>
          <button :class="nodeClass({ kind: 'pending' })" @click="selectNode({ kind: 'pending' })">
            <UIcon name="i-heroicons-clock" class="w-4 h-4" :class="pendingCount ? 'text-amber-500' : 'text-gray-400'" />
            <span class="flex-1 text-left truncate">Pending review</span>
            <span v-if="pendingCount" class="text-[11px] text-amber-600 font-medium tabular-nums">{{ pendingCount }}</span>
          </button>

          <div class="h-px bg-gray-200/70 my-2 mx-1"></div>

          <button :class="nodeClass({ kind: 'global' })" @click="selectNode({ kind: 'global' })">
            <UIcon name="i-heroicons-globe-alt" class="w-4 h-4 text-gray-400" />
            <span class="flex-1 text-left truncate">Global / Org-wide</span>
            <span class="text-[11px] text-gray-400 tabular-nums">{{ globalCount }}</span>
          </button>

          <div class="px-2 pt-3 pb-1 text-[10px] font-semibold uppercase tracking-wider text-gray-400">Agents</div>

          <template v-for="agent in agents" :key="agent.id">
            <div class="group flex items-center">
              <button
                class="shrink-0 w-5 h-7 flex items-center justify-center text-gray-300 hover:text-gray-500"
                @click="toggleAgentExpand(agent.id)"
              >
                <UIcon
                  name="i-heroicons-chevron-right"
                  class="w-3 h-3 transition-transform"
                  :class="{ 'rotate-90': expandedAgents.has(agent.id) }"
                />
              </button>
              <button :class="nodeClass({ kind: 'agent', agentId: agent.id }, true)" @click="selectNode({ kind: 'agent', agentId: agent.id })">
                <span class="w-4 h-4 rounded flex items-center justify-center text-[10px] font-semibold shrink-0"
                      :class="agentBadgeClass(agent.id)">{{ agentInitial(agent.name) }}</span>
                <span class="flex-1 text-left truncate">{{ agent.name }}</span>
                <span v-if="agentPending(agent.id)" class="w-1.5 h-1.5 rounded-full bg-amber-400" title="Has pending changes"></span>
                <span class="text-[11px] text-gray-400 tabular-nums">{{ agentCount(agent.id) }}</span>
              </button>
            </div>
            <!-- Agent → tables -->
            <div v-if="expandedAgents.has(agent.id)" class="ml-6 pl-1 border-l border-gray-200/70 space-y-0.5 mb-1">
              <div v-if="(agentTables[agent.id] || []).length === 0" class="px-2 py-1 text-[11px] text-gray-300 italic">
                {{ tablesLoading.has(agent.id) ? 'Loading tables…' : 'No table-scoped rules' }}
              </div>
              <button
                v-for="t in (agentTables[agent.id] || [])"
                :key="t.key"
                :class="nodeClass({ kind: 'table', agentId: agent.id, tableKey: t.key })"
                @click="selectNode({ kind: 'table', agentId: agent.id, tableKey: t.key })"
              >
                <UIcon name="i-heroicons-table-cells" class="w-3.5 h-3.5 text-gray-400" />
                <span class="flex-1 text-left truncate font-mono text-[11px]">{{ t.label }}</span>
                <span class="text-[11px] text-gray-400 tabular-nums">{{ t.count }}</span>
              </button>
            </div>
          </template>
        </div>
      </aside>

      <!-- ── Pane 2: List ─────────────────────────────── -->
      <section class="w-[380px] shrink-0 border-r border-gray-200 flex flex-col min-h-0">
        <!-- List header / quick filters -->
        <div class="h-11 shrink-0 px-3 flex items-center justify-between border-b border-gray-200/70">
          <div class="flex items-center gap-1.5 min-w-0">
            <UIcon :name="nodeIcon" class="w-4 h-4 text-gray-400 shrink-0" />
            <span class="text-xs font-medium text-gray-700 truncate">{{ nodeTitle }}</span>
            <span class="text-[11px] text-gray-400 tabular-nums">{{ filteredList.length }}</span>
          </div>
          <div class="flex items-center gap-1">
            <button
              v-for="f in statusFilters"
              :key="f.value"
              class="h-6 px-2 rounded text-[11px] font-medium transition-colors"
              :class="statusFilter === f.value ? 'bg-gray-900 text-white' : 'text-gray-500 hover:bg-gray-100'"
              @click="statusFilter = f.value"
            >{{ f.label }}</button>
          </div>
        </div>

        <!-- Bulk bar -->
        <div v-if="selectedIds.size > 0" class="px-3 py-2 border-b border-gray-200/70 bg-gray-50 flex items-center gap-2 text-xs">
          <span class="text-gray-600 font-medium">{{ selectedIds.size }} selected</span>
          <div class="flex-1"></div>
          <button class="h-6 px-2 rounded hover:bg-gray-200 text-gray-600" @click="bulk({ status: 'published' })">Activate</button>
          <button class="h-6 px-2 rounded hover:bg-gray-200 text-gray-600" @click="bulk({ status: 'draft' })">Deactivate</button>
          <UPopover :popper="{ placement: 'bottom-end' }">
            <button class="h-6 px-2 rounded hover:bg-gray-200 text-gray-600 inline-flex items-center gap-1">Attach <UIcon name="i-heroicons-chevron-down" class="w-3 h-3" /></button>
            <template #panel="{ close }">
              <div class="p-1 w-44 max-h-60 overflow-y-auto">
                <button v-for="a in agents" :key="a.id" class="w-full text-left px-2 py-1.5 text-xs rounded hover:bg-gray-100 truncate"
                        @click="bulk({ add_data_source_ids: [a.id] }); close()">{{ a.name }}</button>
              </div>
            </template>
          </UPopover>
          <button class="h-6 px-2 rounded hover:bg-red-50 text-red-500" @click="bulkDelete">Delete</button>
          <button class="h-6 w-6 rounded hover:bg-gray-200 text-gray-400 flex items-center justify-center" @click="clearSelection">
            <UIcon name="i-heroicons-x-mark" class="w-3.5 h-3.5" />
          </button>
        </div>

        <!-- Rows -->
        <div class="flex-1 overflow-y-auto">
          <div v-if="loading" class="p-6 text-center text-xs text-gray-400">Loading…</div>
          <div v-else-if="filteredList.length === 0" class="p-10 text-center">
            <UIcon name="i-heroicons-inbox" class="w-7 h-7 mx-auto text-gray-300" />
            <p class="text-xs text-gray-400 mt-2">No instructions here yet.</p>
          </div>
          <button
            v-for="ins in filteredList"
            :key="ins.id"
            class="w-full text-left px-3 py-2.5 border-b border-gray-100 flex gap-2.5 group transition-colors"
            :class="selectedId === ins.id ? 'bg-blue-50/60' : 'hover:bg-gray-50'"
            @click="openInstruction(ins)"
          >
            <!-- checkbox -->
            <span
              class="shrink-0 mt-0.5 w-4 h-4 rounded border flex items-center justify-center transition-colors"
              :class="selectedIds.has(ins.id) ? 'bg-gray-900 border-gray-900' : 'border-gray-300 opacity-0 group-hover:opacity-100'"
              @click.stop="toggleSelect(ins.id)"
            >
              <UIcon v-if="selectedIds.has(ins.id)" name="i-heroicons-check" class="w-3 h-3 text-white" />
            </span>

            <!-- status dot -->
            <span class="shrink-0 mt-1 w-1.5 h-1.5 rounded-full" :class="h.getStatusIconClass(ins)" :title="h.getStatusTooltip(ins)"></span>

            <!-- content -->
            <div class="flex-1 min-w-0">
              <div class="flex items-center gap-1.5">
                <span class="text-[13px] text-gray-900 font-medium truncate">{{ displayTitle(ins) }}</span>
              </div>
              <p class="text-xs text-gray-500 truncate mt-0.5 leading-snug">{{ preview(ins) }}</p>
              <!-- meta icons -->
              <div class="flex items-center gap-1.5 mt-1.5">
                <span class="inline-flex items-center gap-0.5 text-[10px] text-gray-400" :title="h.getSourceTooltip(ins)">
                  <UIcon :name="h.getSourceIcon(ins)" class="w-3 h-3" />
                </span>
                <span class="inline-flex items-center px-1.5 h-4 rounded text-[10px] font-medium" :class="h.getLoadModeClass(ins.load_mode)">
                  {{ h.getLoadModeLabel(ins.load_mode) }}
                </span>
                <span v-if="scopeIcon(ins)" class="inline-flex items-center gap-0.5 text-[10px] text-gray-400" :title="scopeTooltip(ins)">
                  <UIcon :name="scopeIcon(ins)" class="w-3 h-3" />
                </span>
                <span v-if="ins.data_sources && ins.data_sources.length > 1"
                      class="inline-flex items-center px-1.5 h-4 rounded bg-gray-100 text-gray-500 text-[10px] font-medium"
                      :title="ins.data_sources.map(d => d.name).join(', ')">
                  {{ ins.data_sources.length }} agents
                </span>
                <span v-for="lbl in (ins.labels || []).slice(0,1)" :key="lbl.id"
                      class="inline-flex items-center px-1.5 h-4 rounded text-[10px] font-medium"
                      :style="labelStyle(lbl)">{{ lbl.name }}</span>
              </div>
            </div>
          </button>
        </div>
      </section>

      <!-- ── Pane 3: Detail ───────────────────────────── -->
      <section class="flex-1 min-w-0 flex flex-col">
        <template v-if="detail">
          <!-- detail header -->
          <div class="h-11 shrink-0 px-4 flex items-center justify-between border-b border-gray-200/70">
            <div class="flex items-center gap-2 min-w-0">
              <span class="w-1.5 h-1.5 rounded-full" :class="h.getStatusIconClass(detail)"></span>
              <span class="text-xs font-medium text-gray-500">{{ h.getStatusLabel(detail) }}</span>
            </div>
            <div class="flex items-center gap-1.5">
              <button
                class="h-7 w-7 rounded-md flex items-center justify-center transition-colors"
                :class="showHistory ? 'bg-gray-100 text-gray-700' : 'text-gray-400 hover:bg-gray-100'"
                title="Version history"
                @click="showHistory = !showHistory"
              >
                <UIcon name="i-heroicons-clock" class="w-4 h-4" />
              </button>
              <template v-if="!editing">
                <button class="h-7 px-3 rounded-md border border-gray-200 text-gray-700 text-xs font-medium hover:bg-gray-50" @click="startEdit">Edit</button>
              </template>
              <template v-else>
                <button class="h-7 px-3 rounded-md text-gray-500 text-xs hover:bg-gray-100" @click="cancelEdit">Cancel</button>
                <button class="h-7 px-3 rounded-md bg-gray-900 text-white text-xs font-medium hover:bg-black disabled:opacity-50" :disabled="saving" @click="save">
                  {{ saving ? 'Saving…' : 'Save' }}
                </button>
              </template>
            </div>
          </div>

          <!-- detail body -->
          <div class="flex-1 overflow-y-auto px-6 py-5">
            <!-- title -->
            <input
              v-if="editing"
              v-model="draft.title"
              placeholder="Untitled instruction"
              class="w-full text-lg font-semibold text-gray-900 outline-none placeholder:text-gray-300 mb-3"
            />
            <h2 v-else class="text-lg font-semibold text-gray-900 mb-3">{{ displayTitle(detail) }}</h2>

            <!-- meta controls -->
            <div class="flex flex-wrap items-center gap-2 mb-5">
              <!-- load mode -->
              <USelectMenu
                v-if="editing"
                v-model="draft.load_mode"
                :options="loadModeOptions"
                value-attribute="value"
                option-attribute="label"
                size="xs"
              />
              <span v-else class="inline-flex items-center px-2 h-6 rounded-md text-[11px] font-medium" :class="h.getLoadModeClass(detail.load_mode)">
                <UIcon name="i-heroicons-bolt" class="w-3 h-3 mr-1" />{{ h.getLoadModeLabel(detail.load_mode) }}
              </span>
              <!-- status toggle -->
              <USelectMenu
                v-if="editing"
                v-model="draft.status"
                :options="statusOptions"
                value-attribute="value"
                option-attribute="label"
                size="xs"
              />
              <!-- source -->
              <span class="inline-flex items-center px-2 h-6 rounded-md bg-gray-100 text-gray-500 text-[11px] font-medium">
                <UIcon :name="h.getSourceIcon(detail)" class="w-3 h-3 mr-1" />{{ h.getSourceTooltip(detail) }}
              </span>
            </div>

            <!-- body -->
            <div class="prose-instruction">
              <InstructionEditor
                :key="detail.id + (editing ? '-edit' : '-view')"
                v-model="draft.text"
                mode="wysiwyg"
                :editable="editing"
                :data-source-ids="(detail.data_sources || []).map(d => d.id)"
                :is-all-data-sources="(detail.data_sources || []).length === 0"
                placeholder="Write the instruction in markdown…"
              />
            </div>

            <!-- Other uses / reach -->
            <div class="mt-6 pt-5 border-t border-gray-100">
              <div class="text-[10px] font-semibold uppercase tracking-wider text-gray-400 mb-2">Used by</div>
              <div v-if="(detail.data_sources || []).length === 0" class="flex items-center gap-1.5 text-xs text-gray-500">
                <UIcon name="i-heroicons-globe-alt" class="w-3.5 h-3.5 text-gray-400" />
                All agents (global)
              </div>
              <div v-else class="flex flex-wrap gap-1.5">
                <span v-for="ds in detail.data_sources" :key="ds.id"
                      class="inline-flex items-center gap-1 px-2 h-6 rounded-md bg-gray-100 text-gray-600 text-[11px]">
                  <span class="w-3.5 h-3.5 rounded flex items-center justify-center text-[9px] font-semibold" :class="agentBadgeClass(ds.id)">{{ agentInitial(ds.name) }}</span>
                  {{ ds.name }}
                </span>
              </div>

              <template v-if="(detail.references || []).length">
                <div class="text-[10px] font-semibold uppercase tracking-wider text-gray-400 mt-4 mb-2">Attached to</div>
                <div class="flex flex-wrap gap-1.5">
                  <span v-for="(ref, i) in detail.references" :key="i"
                        class="inline-flex items-center gap-1 px-2 h-6 rounded-md bg-gray-100 text-gray-600 text-[11px] font-mono">
                    <UIcon :name="h.getRefIcon(ref.object_type)" class="w-3 h-3" />
                    {{ refLabel(ref) }}
                  </span>
                </div>
              </template>
            </div>
          </div>
        </template>

        <!-- empty detail -->
        <div v-else class="flex-1 flex flex-col items-center justify-center text-center px-6">
          <UIcon name="i-heroicons-document-text" class="w-9 h-9 text-gray-200" />
          <p class="text-sm text-gray-400 mt-3">Select an instruction to view and edit it.</p>
        </div>
      </section>

      <!-- ── Pane 4: Versions ─────────────────────────── -->
      <aside v-if="detail && showHistory" class="w-60 shrink-0 border-l border-gray-200 flex flex-col bg-gray-50/40">
        <div class="h-11 shrink-0 px-3 flex items-center border-b border-gray-200/70">
          <span class="text-[10px] font-semibold uppercase tracking-wider text-gray-400">Version history</span>
        </div>
        <div class="flex-1 overflow-y-auto p-2 space-y-0.5">
          <div v-if="versionsLoading" class="p-3 text-center text-[11px] text-gray-400">Loading…</div>
          <div v-else-if="versions.length === 0" class="p-3 text-center text-[11px] text-gray-300">No history yet.</div>
          <div
            v-for="(v, i) in versions"
            :key="v.id"
            class="px-2.5 py-2 rounded-md hover:bg-white border border-transparent hover:border-gray-200 transition-colors"
          >
            <div class="flex items-center justify-between">
              <span class="text-xs font-medium text-gray-700">v{{ v.version_number }}</span>
              <span v-if="i === 0" class="text-[9px] font-semibold uppercase tracking-wider text-green-600 bg-green-50 px-1.5 rounded">current</span>
              <button v-else class="text-[10px] text-gray-400 hover:text-gray-700" @click="restore(v)">Restore</button>
            </div>
            <div class="text-[10px] text-gray-400 mt-0.5">{{ fmtDate(v.created_at) }}</div>
          </div>
        </div>
      </aside>
    </div>

    <!-- Create modal (reuse existing) -->
    <InstructionModalComponent
      v-model="showCreate"
      :instruction="null"
      @instruction-saved="onCreated"
    />
  </div>
</template>

<script setup lang="ts">
import InstructionEditor from '~/components/instructions/InstructionEditor.vue'
import InstructionModalComponent from '~/components/InstructionModalComponent.vue'
import { useInstructionHelpers, type Instruction } from '~/composables/useInstructionHelpers'

const h = useInstructionHelpers()
const toast = useToast()

type NodeKind = 'all' | 'skills' | 'pending' | 'global' | 'agent' | 'table'
interface TreeNode { kind: NodeKind; agentId?: string; tableKey?: string }

// ── State ───────────────────────────────────────────────
const allInstructions = ref<Instruction[]>([])
const agents = ref<{ id: string; name: string; type?: string }[]>([])
const loading = ref(false)
const search = ref('')
const statusFilter = ref<'all' | 'published' | 'draft' | 'pending_review'>('all')
const node = ref<TreeNode>({ kind: 'all' })

const expandedAgents = ref<Set<string>>(new Set())
const agentTables = ref<Record<string, { key: string; label: string; count: number }[]>>({})
const tablesLoading = ref<Set<string>>(new Set())

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

// ── Fetching ────────────────────────────────────────────
const fetchAll = async () => {
  loading.value = true
  try {
    const { data } = await useMyFetch<any>('/api/instructions', {
      method: 'GET',
      query: { skip: 0, limit: 200, include_own: true, include_drafts: true, include_archived: true },
    })
    allInstructions.value = data.value?.items || []
  } catch (e) {
    console.error(e)
  } finally {
    loading.value = false
  }
}

const fetchAgents = async () => {
  try {
    const { data } = await useMyFetch<any[]>('/data_sources/active', { method: 'GET' })
    agents.value = (data.value || []).map((d: any) => ({ id: d.id, name: d.name, type: d.type }))
  } catch (e) {
    console.error(e)
  }
}

const loadAgentTables = async (agentId: string) => {
  if (agentTables.value[agentId] || tablesLoading.value.has(agentId)) return
  tablesLoading.value.add(agentId)
  tablesLoading.value = new Set(tablesLoading.value)
  // Derive table-scoped rules from instruction references for this agent.
  const map = new Map<string, { label: string; count: number }>()
  for (const ins of allInstructions.value) {
    const inAgent = (ins.data_sources || []).some(d => d.id === agentId) || (ins.data_sources || []).length === 0
    if (!inAgent) continue
    for (const ref of (ins.references || [])) {
      if (ref.object_type === 'datasource_table') {
        const label = ref.display_text || ref.object?.name || 'table'
        const key = String(ref.object_id || label)
        const cur = map.get(key) || { label, count: 0 }
        cur.count += 1
        map.set(key, cur)
      }
    }
  }
  agentTables.value[agentId] = Array.from(map.entries()).map(([key, v]) => ({ key, label: v.label, count: v.count }))
  tablesLoading.value.delete(agentId)
  tablesLoading.value = new Set(tablesLoading.value)
}

const toggleAgentExpand = (agentId: string) => {
  if (expandedAgents.value.has(agentId)) expandedAgents.value.delete(agentId)
  else { expandedAgents.value.add(agentId); loadAgentTables(agentId) }
  expandedAgents.value = new Set(expandedAgents.value)
}

// ── Tree counts ─────────────────────────────────────────
const isPending = (ins: Instruction) => h.getEffectiveStatus(ins) === 'pending_review'
const pendingCount = computed(() => allInstructions.value.filter(isPending).length)
const globalCount = computed(() => allInstructions.value.filter(i => (i.data_sources || []).length === 0).length)
const skillCount = computed(() => allInstructions.value.filter(i => (i as any).kind === 'skill').length)
const agentCount = (id: string) => allInstructions.value.filter(i => (i.data_sources || []).some(d => d.id === id)).length
const agentPending = (id: string) => allInstructions.value.some(i => isPending(i) && (i.data_sources || []).some(d => d.id === id))

// ── List filtering ──────────────────────────────────────
const filteredList = computed(() => {
  let list = allInstructions.value.slice()
  const n = node.value
  if (n.kind === 'global') list = list.filter(i => (i.data_sources || []).length === 0)
  else if (n.kind === 'pending') list = list.filter(isPending)
  else if (n.kind === 'skills') list = list.filter(i => (i as any).kind === 'skill')
  else if (n.kind === 'agent' && n.agentId) list = list.filter(i => (i.data_sources || []).some(d => d.id === n.agentId))
  else if (n.kind === 'table' && n.tableKey) {
    list = list.filter(i => (i.references || []).some((r: any) => r.object_type === 'datasource_table' && String(r.object_id) === n.tableKey))
  }

  if (statusFilter.value !== 'all') {
    if (statusFilter.value === 'pending_review') list = list.filter(isPending)
    else list = list.filter(i => i.status === statusFilter.value && !isPending(i))
  }

  const q = search.value.trim().toLowerCase()
  if (q) list = list.filter(i => (i.title || '').toLowerCase().includes(q) || (i.text || '').toLowerCase().includes(q))

  return list
})

// ── Node selection ──────────────────────────────────────
const selectNode = (n: TreeNode) => {
  node.value = n
  if (n.kind === 'table' && n.agentId) loadAgentTables(n.agentId)
}
const nodeClass = (n: TreeNode, withChevron = false) => {
  const active =
    node.value.kind === n.kind &&
    node.value.agentId === n.agentId &&
    node.value.tableKey === n.tableKey
  return [
    'flex-1 flex items-center gap-2 h-7 px-2 rounded-md text-xs transition-colors min-w-0',
    withChevron ? '' : 'w-full',
    active ? 'bg-gray-200/70 text-gray-900 font-medium' : 'text-gray-600 hover:bg-gray-100/70',
  ]
}
const nodeTitle = computed(() => {
  const n = node.value
  if (n.kind === 'all') return 'All instructions'
  if (n.kind === 'skills') return 'Skills'
  if (n.kind === 'pending') return 'Pending review'
  if (n.kind === 'global') return 'Global / Org-wide'
  if (n.kind === 'agent') return agents.value.find(a => a.id === n.agentId)?.name || 'Agent'
  if (n.kind === 'table') {
    const t = (agentTables.value[n.agentId || ''] || []).find(x => x.key === n.tableKey)
    return t?.label || 'Table'
  }
  return 'Instructions'
})
const nodeIcon = computed(() => {
  const n = node.value
  if (n.kind === 'pending') return 'i-heroicons-clock'
  if (n.kind === 'global') return 'i-heroicons-globe-alt'
  if (n.kind === 'skills') return 'i-heroicons-sparkles'
  if (n.kind === 'agent') return 'i-heroicons-cpu-chip'
  if (n.kind === 'table') return 'i-heroicons-table-cells'
  return 'i-heroicons-square-3-stack-3d'
})

// ── Detail ──────────────────────────────────────────────
const openInstruction = async (ins: Instruction) => {
  selectedId.value = ins.id
  detail.value = ins
  editing.value = false
  syncDraft(ins)
  loadVersions(ins.id)
  // hydrate full record (references etc.)
  try {
    const { data } = await useMyFetch<Instruction>(`/api/instructions/${ins.id}`, { method: 'GET' })
    if (data.value && selectedId.value === ins.id) {
      detail.value = data.value
      if (!editing.value) syncDraft(data.value)
    }
  } catch (e) { /* keep list copy */ }
}
const syncDraft = (ins: Instruction) => {
  draft.title = ins.title || ''
  draft.text = ins.text || ''
  draft.load_mode = ins.load_mode || 'always'
  draft.status = ins.status || 'published'
}
const startEdit = () => { if (detail.value) { syncDraft(detail.value); editing.value = true } }
const cancelEdit = () => { if (detail.value) syncDraft(detail.value); editing.value = false }

const save = async () => {
  if (!detail.value) return
  saving.value = true
  try {
    const { data, error } = await useMyFetch<Instruction>(`/api/instructions/${detail.value.id}`, {
      method: 'PUT',
      body: { title: draft.title || null, text: draft.text, load_mode: draft.load_mode, status: draft.status },
    })
    if (error.value) throw new Error((error.value as any)?.message || 'Save failed')
    toast.add({ title: 'Saved', color: 'green' })
    editing.value = false
    if (data.value) detail.value = { ...detail.value, ...data.value }
    await fetchAll()
    const fresh = allInstructions.value.find(i => i.id === detail.value?.id)
    if (fresh) { detail.value = fresh; syncDraft(fresh) }
    loadVersions(detail.value!.id)
  } catch (e: any) {
    toast.add({ title: 'Error', description: e.message, color: 'red' })
  } finally {
    saving.value = false
  }
}

// ── Versions ────────────────────────────────────────────
const loadVersions = async (id: string) => {
  versionsLoading.value = true
  versions.value = []
  try {
    const { data } = await useMyFetch<any>(`/api/instructions/${id}/versions`, { method: 'GET', query: { limit: 50 } })
    versions.value = data.value?.items || []
  } catch (e) { /* none */ } finally {
    versionsLoading.value = false
  }
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
  } catch (e: any) {
    toast.add({ title: 'Error', description: e?.message, color: 'red' })
  }
}

// ── Multi-select / bulk ─────────────────────────────────
const toggleSelect = (id: string) => {
  if (selectedIds.value.has(id)) selectedIds.value.delete(id)
  else selectedIds.value.add(id)
  selectedIds.value = new Set(selectedIds.value)
}
const clearSelection = () => { selectedIds.value = new Set() }
const bulk = async (updates: Record<string, any>) => {
  const ids = Array.from(selectedIds.value)
  if (!ids.length) return
  try {
    await useMyFetch('/api/instructions/bulk', { method: 'PUT', body: { ids, ...updates } })
    toast.add({ title: `Updated ${ids.length}`, color: 'green' })
    clearSelection()
    await fetchAll()
  } catch (e: any) {
    toast.add({ title: 'Error', description: e?.message, color: 'red' })
  }
}
const bulkDelete = async () => {
  const ids = Array.from(selectedIds.value)
  if (!ids.length) return
  if (!window.confirm(`Delete ${ids.length} instruction(s)?`)) return
  try {
    await useMyFetch('/api/instructions/bulk', { method: 'DELETE', body: { ids } })
    toast.add({ title: `Deleted ${ids.length}`, color: 'green' })
    if (selectedId.value && ids.includes(selectedId.value)) { detail.value = null; selectedId.value = null }
    clearSelection()
    await fetchAll()
  } catch (e: any) {
    toast.add({ title: 'Error', description: e?.message, color: 'red' })
  }
}

// ── Create ──────────────────────────────────────────────
const openCreate = () => { showCreate.value = true }
const onCreated = async () => { showCreate.value = false; await fetchAll() }

// ── Display helpers ─────────────────────────────────────
const displayTitle = (ins: Instruction) => ins.title || (ins.text || '').split('\n')[0].slice(0, 60) || 'Untitled'
const preview = (ins: Instruction) => {
  const body = (ins.text || '').replace(/[#*`>_\-]/g, ' ').replace(/\s+/g, ' ').trim()
  return ins.title ? body.slice(0, 90) : body.slice(60, 150)
}
const scopeIcon = (ins: Instruction) => {
  if ((ins.references || []).some((r: any) => r.object_type === 'datasource_table')) return 'i-heroicons-table-cells'
  if ((ins.data_sources || []).length === 0) return 'i-heroicons-globe-alt'
  return ''
}
const scopeTooltip = (ins: Instruction) => {
  if ((ins.data_sources || []).length === 0) return 'Global — applies to all agents'
  return h.getDataSourceTooltip(ins)
}
const refLabel = (ref: any) => ref.display_text || ref.object?.name || ref.object_type
const fmtDate = (s?: string) => {
  if (!s) return ''
  try { return new Date(s).toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) } catch { return s }
}
const labelStyle = (lbl: any) => lbl.color ? { backgroundColor: lbl.color + '22', color: lbl.color } : { backgroundColor: '#f3f4f6', color: '#6b7280' }

// agent avatar
const AGENT_COLORS = ['bg-blue-100 text-blue-700', 'bg-emerald-100 text-emerald-700', 'bg-violet-100 text-violet-700', 'bg-amber-100 text-amber-700', 'bg-rose-100 text-rose-700', 'bg-cyan-100 text-cyan-700']
const agentBadgeClass = (id: string) => {
  const idx = agents.value.findIndex(a => a.id === id)
  return AGENT_COLORS[(idx >= 0 ? idx : 0) % AGENT_COLORS.length]
}
const agentInitial = (name: string) => (name || '?').trim().charAt(0).toUpperCase()

// ── Init ────────────────────────────────────────────────
onMounted(async () => {
  await Promise.all([fetchAgents(), fetchAll()])
})
</script>

<style scoped>
.prose-instruction :deep(.tiptap-prose) { min-height: 80px; }
</style>
