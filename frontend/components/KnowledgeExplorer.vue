<template>
  <div class="flex flex-col h-[calc(100vh-64px)] text-sm">
    <!-- Header -->
    <div class="flex items-center justify-between pl-3 pr-4 py-3 shrink-0">
      <div>
        <h1 class="text-[15px] font-semibold text-gray-900 tracking-tight">Agents</h1>
        <p class="text-xs text-gray-400 mt-0.5">The instructions, rules and skills your agents reason with.</p>
      </div>
      <div class="flex items-center gap-2">
        <button v-if="pendingCount > 0" class="inline-flex items-center gap-1.5 h-8 px-3 rounded-md border border-amber-200 bg-amber-50 text-amber-700 text-xs font-medium hover:bg-amber-100 transition-colors" @click="expand('pending', true)">
          <span class="w-1.5 h-1.5 rounded-full bg-amber-500"></span>{{ pendingCount }} pending review
        </button>
        <GitConnectionButton :has-connection="gitRepos.length > 0" :connected-repos="gitRepos" :last-indexed-at="gitLastIndexed" @click="showGitModal = true" />
        <button class="inline-flex items-center gap-1.5 h-8 px-3 rounded-md bg-gray-900 text-white text-xs font-medium hover:bg-black transition-colors" @click="openCreate()">
          <UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" /> Add instruction
        </button>
      </div>
    </div>

    <!-- Body: tree → detail → versions -->
    <div class="flex-1 min-h-0 flex border-t border-gray-200">
      <!-- ── Pane 1: Tree ───────────────────────────────── -->
      <aside class="w-[300px] shrink-0 border-r border-gray-200 flex flex-col">
        <div class="px-2 pt-2.5 pb-2 flex items-center gap-1.5">
          <div class="relative flex-1">
            <UIcon name="i-heroicons-magnifying-glass" class="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400" />
            <input v-model="search" type="text" placeholder="Search everything…" class="w-full h-8 pl-8 pr-2 text-xs bg-gray-50 border border-gray-200 rounded-md outline-none focus:border-gray-400 focus:bg-white placeholder:text-gray-400" />
          </div>
          <UPopover :popper="{ placement: 'bottom-end' }" :ui="{ ring: '', shadow: 'shadow-md' }">
            <button type="button" class="relative h-8 w-8 flex items-center justify-center rounded-md border border-gray-200 text-gray-500 hover:bg-gray-50" title="Filters">
              <UIcon name="i-heroicons-adjustments-horizontal" class="w-4 h-4" />
              <span v-if="activeFilterCount" class="absolute -top-1 -right-1 w-3.5 h-3.5 rounded-full bg-gray-900 text-white text-[8px] font-semibold flex items-center justify-center">{{ activeFilterCount }}</span>
            </button>
            <template #panel="{ close }">
              <div class="p-3 w-56 space-y-3">
                <FilterSection label="Status" :options="statusOpts" v-model="fStatus" />
                <FilterSection label="Loading" :options="loadOpts" v-model="fLoad" />
                <FilterSection label="Source" :options="sourceOpts" v-model="fSource" />
                <FilterSection v-if="categoryOpts.length" label="Category" :options="categoryOpts" v-model="fCategory" />
                <div class="flex items-center justify-between pt-1 border-t border-gray-100">
                  <button class="text-[11px] text-gray-500 hover:text-gray-800" @click="clearFilters">Clear all</button>
                  <button class="text-[11px] font-medium text-gray-900" @click="close && close()">Done</button>
                </div>
              </div>
            </template>
          </UPopover>
        </div>

        <div class="flex-1 overflow-y-auto px-2 pb-2 space-y-0.5">
          <TreeGroup label="All instructions" icon="i-heroicons-square-3-stack-3d" :count="allInstructions.length" :open="isOpen('all')" @toggle="expand('all')">
            <InstrLeaf v-for="ins in listFor('all')" :key="ins.id" :ins="ins" />
          </TreeGroup>
          <TreeGroup label="Skills" icon="i-heroicons-sparkles" :count="skillCount" :open="isOpen('skills')" @toggle="expand('skills')">
            <EmptyHint v-if="skillCount === 0" text="No skills yet." />
            <InstrLeaf v-for="ins in listFor('skills')" :key="ins.id" :ins="ins" />
          </TreeGroup>
          <TreeGroup label="Pending review" icon="i-heroicons-clock" :count="pendingCount" :count-accent="pendingCount > 0" :open="isOpen('pending')" @toggle="expand('pending')">
            <EmptyHint v-if="pendingCount === 0" text="Nothing awaiting review." />
            <InstrLeaf v-for="ins in listFor('pending')" :key="ins.id" :ins="ins" />
          </TreeGroup>

          <div class="h-px bg-gray-100 my-2 mx-1"></div>

          <TreeGroup label="Global / Org-wide" icon="i-heroicons-globe-alt" :count="globalCount" addable :open="isOpen('global')" @toggle="expand('global')" @add="openCreate()">
            <EmptyHint v-if="listFor('global').length === 0" text="No global rules." add @add="openCreate()" />
            <InstrLeaf v-for="ins in listFor('global')" :key="ins.id" :ins="ins" />
          </TreeGroup>

          <div class="px-2 pt-3 pb-1 text-[10px] font-semibold uppercase tracking-wider text-gray-400">Agents</div>

          <template v-for="agent in agents" :key="agent.id">
            <TreeGroup :label="agent.name" :count="agentCount(agent.id)" :pending="agentPending(agent.id)" :badge="needsSignIn(agent) ? 'Sign in' : ''" :disabled="needsSignIn(agent)" :open="isOpen('agent:' + agent.id)" @toggle="expand('agent:' + agent.id)" @badge="openAgentTab(agent.id)">
              <template #icon><DataSourceIcon :type="agent.type" class="w-4 h-4 shrink-0" /></template>

              <TreeGroup label="Tables" icon="i-heroicons-table-cells" :count="agentTables[agent.id]?.length" :indent="1" :open="isOpen('tables:' + agent.id)" @toggle="expand('tables:' + agent.id)">
                <TreeGroup v-for="t in (agentTables[agent.id] || [])" :key="t.id" :label="t.name" :icon="t.is_active ? 'i-heroicons-check-circle' : 'i-heroicons-table-cells'" :count="listForTable(agent.id, t.id).length || undefined" mono addable :indent="2" :open="isOpen('table:' + agent.id + ':' + t.id)" @toggle="expand('table:' + agent.id + ':' + t.id)" @add="openCreate({ agentId: agent.id, tableId: t.id, tableName: t.name })">
                  <InstrLeaf v-for="ins in listForTable(agent.id, t.id)" :key="ins.id" :ins="ins" :indent="3" />
                  <EmptyHint v-if="listForTable(agent.id, t.id).length === 0" text="No rules attached." add @add="openCreate({ agentId: agent.id, tableId: t.id, tableName: t.name })" :pad="62" />
                </TreeGroup>
                <EmptyHint v-if="(agentTables[agent.id]?.length ?? -1) === 0" text="No accessible tables." :pad="48" />
              </TreeGroup>

              <TreeGroup label="Tools" icon="i-heroicons-wrench-screwdriver" :count="agentTools[agent.id]?.length" :indent="1" :open="isOpen('tools:' + agent.id)" @toggle="expand('tools:' + agent.id)">
                <div v-for="tool in (agentTools[agent.id] || [])" :key="tool.id || tool.name" class="flex items-center gap-2 h-7 rounded-md text-xs text-gray-600" style="padding-left:48px;padding-right:8px">
                  <UIcon name="i-heroicons-wrench-screwdriver" class="w-3 h-3 text-gray-300 shrink-0" />
                  <span class="flex-1 text-left truncate font-mono text-[11px]">{{ tool.name }}</span>
                  <span v-if="tool.is_enabled === false" class="text-[9px] px-1 rounded bg-gray-100 text-gray-400">off</span>
                  <span v-else-if="tool.policy && tool.policy !== 'allow'" class="text-[9px] px-1 rounded bg-gray-100 text-gray-500">{{ tool.policy }}</span>
                </div>
                <EmptyHint v-if="(agentTools[agent.id]?.length ?? -1) === 0" text="No tools connected." :pad="48" />
              </TreeGroup>

              <TreeGroup label="Instructions" icon="i-heroicons-document-text" :count="listForAgent(agent.id).length" addable :indent="1" :open="isOpen('instr:' + agent.id)" @toggle="expand('instr:' + agent.id)" @add="openCreate({ agentId: agent.id })">
                <InstrLeaf v-for="ins in listForAgent(agent.id)" :key="ins.id" :ins="ins" :indent="2" />
                <EmptyHint v-if="listForAgent(agent.id).length === 0" text="No instructions yet." add @add="openCreate({ agentId: agent.id })" :pad="48" />
              </TreeGroup>
            </TreeGroup>
          </template>
        </div>

        <!-- Connections footer -->
        <div v-if="connections.length" class="border-t border-gray-200 px-3 py-2 flex items-center gap-2">
          <span class="text-[10px] font-semibold uppercase tracking-wider text-gray-400 mr-1">Connections</span>
          <UTooltip v-for="c in connections" :key="c.id" :text="`${c.name} · ${c.type}`">
            <a :href="'/agents'" target="_blank" class="relative inline-flex items-center justify-center w-6 h-6 rounded-md border border-gray-200 hover:bg-gray-50">
              <DataSourceIcon :type="c.type" class="w-3.5 h-3.5" />
              <span class="absolute -bottom-0.5 -right-0.5 w-1.5 h-1.5 rounded-full" :class="c.is_active === false ? 'bg-gray-300' : 'bg-green-500'"></span>
            </a>
          </UTooltip>
        </div>
      </aside>

      <!-- ── Pane 2: Detail ───────────────────────────── -->
      <section class="flex-1 min-w-0 flex flex-col">
        <template v-if="detail || creating">
          <div class="h-11 shrink-0 px-4 flex items-center justify-between border-b border-gray-100">
            <div class="flex items-center gap-2 min-w-0">
              <template v-if="creating">
                <span class="text-xs font-medium text-gray-500">New instruction</span>
              </template>
              <template v-else>
                <span class="w-1.5 h-1.5 rounded-full" :class="h.getStatusIconClass(detail)"></span>
                <span class="text-xs font-medium text-gray-500">{{ h.getStatusLabel(detail) }}</span>
              </template>
            </div>
            <div class="flex items-center gap-1.5">
              <button v-if="!creating" class="h-7 w-7 rounded-md flex items-center justify-center transition-colors" :class="showHistory ? 'bg-gray-100 text-gray-700' : 'text-gray-400 hover:bg-gray-100'" title="Version history" @click="showHistory = !showHistory">
                <UIcon name="i-heroicons-clock" class="w-4 h-4" />
              </button>
              <template v-if="!editing">
                <button class="h-7 px-3 rounded-md border border-gray-200 text-gray-700 text-xs font-medium hover:bg-gray-50" @click="startEdit">Edit</button>
              </template>
              <template v-else>
                <button class="h-7 px-3 rounded-md text-gray-500 text-xs hover:bg-gray-100" @click="cancelEdit">Cancel</button>
                <button class="h-7 px-3 rounded-md bg-gray-900 text-white text-xs font-medium hover:bg-black disabled:opacity-50" :disabled="saving" @click="save">{{ saving ? 'Saving…' : (creating ? 'Create' : 'Save') }}</button>
              </template>
            </div>
          </div>

          <div class="flex-1 overflow-y-auto px-8 py-6 max-w-3xl">
            <!-- Title -->
            <input v-if="editing" v-model="draft.title" placeholder="Untitled instruction" class="w-full text-lg font-semibold text-gray-900 outline-none placeholder:text-gray-300 mb-4" />
            <h2 v-else class="text-lg font-semibold text-gray-900 mb-4">{{ displayTitle(detail) }}</h2>

            <!-- View-mode property chips -->
            <div v-if="!editing" class="flex flex-wrap items-center gap-2 mb-5">
              <span class="inline-flex items-center px-2 h-6 rounded-md bg-gray-100 text-gray-600 text-[11px] font-medium"><UIcon name="i-heroicons-bolt" class="w-3 h-3 mr-1 text-gray-400" />{{ h.getLoadModeLabel(detail.load_mode) }}</span>
              <span class="inline-flex items-center px-2 h-6 rounded-md bg-gray-100 text-gray-600 text-[11px] font-medium">{{ h.formatCategory(detail.category) }}</span>
              <span class="inline-flex items-center px-2 h-6 rounded-md bg-gray-100 text-gray-600 text-[11px] font-medium"><UIcon :name="h.getSourceIcon(detail)" class="w-3 h-3 mr-1 text-gray-400" />{{ h.getSourceTooltip(detail) }}</span>
            </div>

            <!-- Body -->
            <div class="prose-instruction">
              <InstructionEditor :key="(detail?.id || 'new') + (editing ? '-edit' : '-view')" v-model="draft.text" mode="wysiwyg" :editable="editing" :data-source-ids="draft.data_source_ids" :is-all-data-sources="draft.data_source_ids.length === 0" placeholder="Write the instruction in markdown… (type @ to mention a table or instruction)" />
            </div>

            <!-- Edit-mode properties (below a separator, like the agent panel) -->
            <div v-if="editing" class="mt-6 pt-5 border-t border-gray-100">
              <div class="grid grid-cols-[84px_1fr] gap-x-3 gap-y-2.5 items-center">
                <span class="text-[11px] text-gray-400">Status</span>
                <div><KSelect v-model="draft.status" :options="statusEditOpts" /></div>
                <span class="text-[11px] text-gray-400">Loading</span>
                <div><KSelect v-model="draft.load_mode" :options="loadOpts" icon="i-heroicons-bolt" /></div>
                <span class="text-[11px] text-gray-400">Category</span>
                <div><KSelect v-model="draft.category" :options="categoryOpts" placeholder="General" /></div>
                <span class="text-[11px] text-gray-400">Agents</span>
                <div><KSelect v-model="draft.data_source_ids" :options="agentOpts" multiple placeholder="All agents (global)" icon="i-heroicons-cpu-chip" /></div>
                <template v-if="labelOpts.length">
                  <span class="text-[11px] text-gray-400">Labels</span>
                  <div><KSelect v-model="draft.label_ids" :options="labelOpts" multiple placeholder="None" icon="i-heroicons-tag" /></div>
                </template>
                <template v-if="createRefs.length">
                  <span class="text-[11px] text-gray-400">Attached to</span>
                  <div class="flex flex-wrap gap-1.5">
                    <span v-for="(r, i) in createRefs" :key="i" class="inline-flex items-center gap-1 px-2 h-6 rounded-md bg-gray-100 text-gray-600 text-[11px] font-mono"><UIcon name="i-heroicons-table-cells" class="w-3 h-3 text-gray-400" />{{ r.display_text }}</span>
                  </div>
                </template>
              </div>
            </div>

            <!-- View-mode reach -->
            <div v-if="!editing && detail" class="mt-6 pt-5 border-t border-gray-100">
              <div class="text-[10px] font-semibold uppercase tracking-wider text-gray-400 mb-2">Used by</div>
              <div v-if="(detail.data_sources || []).length === 0" class="flex items-center gap-1.5 text-xs text-gray-500"><UIcon name="i-heroicons-globe-alt" class="w-3.5 h-3.5 text-gray-400" /> All agents (global)</div>
              <div v-else class="flex flex-wrap gap-1.5">
                <span v-for="ds in detail.data_sources" :key="ds.id" class="inline-flex items-center gap-1.5 px-2 h-6 rounded-md bg-gray-100 text-gray-600 text-[11px]"><DataSourceIcon :type="ds.type" class="w-3 h-3" /> {{ ds.name }}</span>
              </div>
              <template v-if="(detail.references || []).length">
                <div class="text-[10px] font-semibold uppercase tracking-wider text-gray-400 mt-4 mb-2">Attached to</div>
                <div class="flex flex-wrap gap-1.5">
                  <span v-for="(ref, i) in detail.references" :key="i" class="inline-flex items-center gap-1 px-2 h-6 rounded-md bg-gray-100 text-gray-600 text-[11px] font-mono"><UIcon :name="h.getRefIcon(ref.object_type)" class="w-3 h-3 text-gray-400" /> {{ refLabel(ref) }}</span>
                </div>
              </template>
            </div>
          </div>
        </template>

        <div v-else class="flex-1 flex items-center justify-center px-6">
          <div class="relative w-full max-w-lg h-72 overflow-hidden">
            <img src="/assets/empty-states/empty-integrations.png" alt="" class="absolute inset-x-0 bottom-0 w-full opacity-80 select-none pointer-events-none" />
            <div class="absolute inset-x-0 bottom-0 flex flex-col items-center justify-center text-center px-6 pb-2">
              <div class="w-12 h-12 flex items-center justify-center rounded-xl bg-white/70 backdrop-blur-sm ring-1 ring-gray-200/70 shadow-sm"><UIcon name="i-heroicons-book-open" class="w-5 h-5 text-gray-400" /></div>
              <h3 class="mt-3 text-[15px] font-medium text-gray-900">Your team's knowledge</h3>
              <p class="mt-1.5 max-w-xs text-sm leading-relaxed text-gray-500">Open an agent and pick an instruction to view, edit, and track its versions.</p>
            </div>
          </div>
        </div>
      </section>

      <!-- ── Pane 3: Versions ─────────────────────────── -->
      <aside v-if="detail && !creating && showHistory" class="w-60 shrink-0 border-l border-gray-200 flex flex-col">
        <div class="h-11 shrink-0 px-3 flex items-center border-b border-gray-100"><span class="text-[10px] font-semibold uppercase tracking-wider text-gray-400">Version history</span></div>
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

    <GitRepoModalComponent v-model="showGitModal" @changed="onGitChanged" />
  </div>
</template>

<script setup lang="ts">
import { h as createElement } from 'vue'
import InstructionEditor from '~/components/instructions/InstructionEditor.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import KSelect from '~/components/KSelect.vue'
import GitConnectionButton from '~/components/instructions/GitConnectionButton.vue'
import GitRepoModalComponent from '~/components/GitRepoModalComponent.vue'
import { useInstructionHelpers, type Instruction } from '~/composables/useInstructionHelpers'

const h = useInstructionHelpers()
const toast = useToast()

// ── State ───────────────────────────────────────────────
const allInstructions = ref<Instruction[]>([])
const agents = ref<any[]>([])
const labels = ref<{ id: string; name: string }[]>([])
const categories = ref<string[]>([])
const search = ref('')

const fStatus = ref<string[]>([]); const fLoad = ref<string[]>([]); const fSource = ref<string[]>([]); const fCategory = ref<string[]>([])

const expanded = ref<Set<string>>(new Set())
const agentTables = ref<Record<string, { id: string; name: string; is_active: boolean }[]>>({})
const agentTools = ref<Record<string, any[]>>({})
const agentLoaded = ref<Set<string>>(new Set())

const selectedId = ref<string | null>(null)
const detail = ref<Instruction | null>(null)
const editing = ref(false)
const creating = ref(false)
const saving = ref(false)
const createRefs = ref<any[]>([])
const draft = reactive<{ title: string; text: string; load_mode: string; status: string; category: string; data_source_ids: string[]; label_ids: string[] }>(
  { title: '', text: '', load_mode: 'always', status: 'published', category: 'general', data_source_ids: [], label_ids: [] }
)

const showHistory = ref(true)
const versions = ref<any[]>([])
const versionsLoading = ref(false)

// git
const gitRepos = ref<{ provider: string; repoName: string }[]>([])
const gitLastIndexed = ref<string | null>(null)
const showGitModal = ref(false)

const statusOpts = [{ value: 'published', label: 'Active' }, { value: 'draft', label: 'Inactive' }, { value: 'pending_review', label: 'Pending review' }]
const statusEditOpts = [{ value: 'published', label: 'Active' }, { value: 'draft', label: 'Inactive' }]
const loadOpts = [{ value: 'always', label: 'Always' }, { value: 'intelligent', label: 'Smart' }, { value: 'disabled', label: 'Off' }]
const sourceOpts = [{ value: 'user', label: 'User' }, { value: 'ai', label: 'AI' }, { value: 'git', label: 'Git' }]
const categoryOpts = computed(() => categories.value.map(c => ({ value: c, label: h.formatCategory(c) })))
const agentOpts = computed(() => agents.value.map(a => ({ value: a.id, label: a.name })))
const labelOpts = computed(() => labels.value.map(l => ({ value: l.id, label: l.name })))
const activeFilterCount = computed(() => fStatus.value.length + fLoad.value.length + fSource.value.length + fCategory.value.length)
const clearFilters = () => { fStatus.value = []; fLoad.value = []; fSource.value = []; fCategory.value = [] }

// connections (deduped from agents)
const connections = computed(() => { const m = new Map<string, any>(); for (const a of agents.value) for (const c of (a.connections || [])) if (!m.has(c.id)) m.set(c.id, c); return Array.from(m.values()) })

// requires sign-in (ported from /agents/index.vue)
const requiresUserAuth = (a: any) => (a.connections || []).some((c: any) => c.auth_policy === 'user_required')
const needsSignIn = (a: any) => {
  if (!requiresUserAuth(a)) return false
  for (const c of (a.connections || [])) {
    if (c.auth_policy === 'user_required' && !c.user_status?.has_user_credentials && c.user_status?.effective_auth !== 'system') return true
  }
  return false
}
const openAgentTab = (id: string) => { window.open(`/agents/${id}/connection`, '_blank') }

// ── Expansion ───────────────────────────────────────────
const isOpen = (key: string) => expanded.value.has(key)
const expand = (key: string, force?: boolean) => {
  if (force) expanded.value.add(key)
  else if (expanded.value.has(key)) expanded.value.delete(key)
  else expanded.value.add(key)
  if (key.startsWith('agent:') && expanded.value.has(key)) { const id = key.slice('agent:'.length); expanded.value.add('instr:' + id); loadAgentMeta(id) }
  expanded.value = new Set(expanded.value)
}

// ── Fetching ────────────────────────────────────────────
const fetchAll = async () => {
  try { const { data } = await useMyFetch<any>('/api/instructions', { method: 'GET', query: { skip: 0, limit: 200, include_own: true, include_drafts: true, include_archived: true } }); allInstructions.value = data.value?.items || [] } catch (e) { console.error(e) }
}
const fetchAgents = async () => {
  try { const { data } = await useMyFetch<any[]>('/data_sources/active', { method: 'GET' }); agents.value = (data.value || []).map((d: any) => ({ id: d.id, name: d.name, type: d.type, connections: d.connections || [], user_status: d.user_status })) } catch (e) { console.error(e) }
}
const fetchLabels = async () => { try { const { data } = await useMyFetch<any[]>('/instructions/labels', { method: 'GET' }); labels.value = data.value || [] } catch {} }
const fetchCategories = async () => { try { const { data } = await useMyFetch<string[]>('/instructions/categories', { method: 'GET' }); categories.value = data.value || [] } catch {} }
const fetchGitStatus = async () => {
  try {
    const { data } = await useMyFetch<any[]>('/git/repositories', { method: 'GET' })
    const repos = data.value || []
    gitRepos.value = repos.map((r: any) => ({ provider: r.provider, repoName: (r.repo_url || '').split('/').pop()?.replace(/\.git$/, '') || 'Repo' }))
    gitLastIndexed.value = repos.map((r: any) => r.last_indexed_at).filter(Boolean).sort().pop() || null
  } catch {}
}
const onGitChanged = () => { fetchGitStatus(); fetchAll() }
const loadAgentMeta = async (id: string) => {
  if (agentLoaded.value.has(id)) return
  agentLoaded.value.add(id)
  try { const { data } = await useMyFetch<any>(`/data_sources/${id}/full_schema`, { method: 'GET' }); const items = Array.isArray(data.value) ? data.value : (data.value?.items || []); agentTables.value[id] = items.map((t: any) => ({ id: String(t.id ?? t.name), name: t.name, is_active: t.is_active !== false })) } catch { agentTables.value[id] = [] }
  try { const { data } = await useMyFetch<any[]>(`/data_sources/${id}/tools`, { method: 'GET' }); agentTools.value[id] = data.value || [] } catch { agentTools.value[id] = [] }
  agentTables.value = { ...agentTables.value }; agentTools.value = { ...agentTools.value }
}

// ── Counts ──────────────────────────────────────────────
const isPending = (ins: Instruction) => h.getEffectiveStatus(ins) === 'pending_review'
const pendingCount = computed(() => allInstructions.value.filter(isPending).length)
const globalCount = computed(() => allInstructions.value.filter(i => (i.data_sources || []).length === 0).length)
const skillCount = computed(() => allInstructions.value.filter(i => (i as any).kind === 'skill').length)
const agentCount = (id: string) => allInstructions.value.filter(i => (i.data_sources || []).some(d => d.id === id)).length
const agentPending = (id: string) => allInstructions.value.some(i => isPending(i) && (i.data_sources || []).some(d => d.id === id))

// ── Leaf lists ──────────────────────────────────────────
const applyFilters = (list: Instruction[]) => {
  let out = list
  if (fStatus.value.length) out = out.filter(i => fStatus.value.includes(isPending(i) ? 'pending_review' : i.status))
  if (fLoad.value.length) out = out.filter(i => fLoad.value.includes(i.load_mode || 'always'))
  if (fSource.value.length) out = out.filter(i => fSource.value.includes(h.getSourceType(i)))
  if (fCategory.value.length) out = out.filter(i => fCategory.value.includes(i.category))
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
const listForTable = (agentId: string, tableId: string) => applyFilters(allInstructions.value.filter(i => (i.data_sources || []).some(d => d.id === agentId) && (i.references || []).some((r: any) => r.object_type === 'datasource_table' && String(r.object_id) === tableId)))

// ── Detail / create ─────────────────────────────────────
const openInstruction = async (ins: Instruction) => {
  creating.value = false; createRefs.value = []
  selectedId.value = ins.id; detail.value = ins; editing.value = false
  syncDraft(ins); loadVersions(ins.id)
  try {
    const { data } = await useMyFetch<Instruction>(`/api/instructions/${ins.id}`, { method: 'GET' })
    if (data.value && selectedId.value === ins.id) {
      detail.value = data.value; if (!editing.value) syncDraft(data.value)
      // keep the tree leaf consistent with the hydrated build/status
      const idx = allInstructions.value.findIndex(i => i.id === ins.id)
      if (idx >= 0) { allInstructions.value[idx] = { ...allInstructions.value[idx], status: data.value.status, current_build_id: data.value.current_build_id, current_build_status: data.value.current_build_status }; allInstructions.value = [...allInstructions.value] }
    }
  } catch (e) {}
}
const syncDraft = (ins: Instruction) => {
  draft.title = ins.title || ''; draft.text = ins.text || ''
  draft.load_mode = ins.load_mode || 'always'; draft.status = ins.status || 'published'
  draft.category = ins.category || 'general'
  draft.data_source_ids = (ins.data_sources || []).map(d => d.id)
  draft.label_ids = (ins.labels || []).map((l: any) => l.id)
}
const openCreate = (scope?: { agentId?: string; tableId?: string; tableName?: string }) => {
  detail.value = null; selectedId.value = null; versions.value = []
  creating.value = true; editing.value = true
  draft.title = ''; draft.text = ''; draft.load_mode = 'always'; draft.status = 'published'; draft.category = 'general'
  draft.data_source_ids = scope?.agentId ? [scope.agentId] : []
  draft.label_ids = []
  createRefs.value = scope?.tableId ? [{ object_type: 'datasource_table', object_id: scope.tableId, relation_type: 'scope', display_text: scope.tableName }] : []
}
const startEdit = () => { if (detail.value) { syncDraft(detail.value); editing.value = true } }
const cancelEdit = () => { if (creating.value) { creating.value = false; editing.value = false; createRefs.value = [] } else { if (detail.value) syncDraft(detail.value); editing.value = false } }
const save = async () => {
  saving.value = true
  try {
    const body: any = { title: draft.title || null, text: draft.text, load_mode: draft.load_mode, status: draft.status, category: draft.category, data_source_ids: draft.data_source_ids, label_ids: draft.label_ids }
    if (creating.value) {
      body.references = createRefs.value
      const endpoint = draft.data_source_ids.length ? '/api/instructions' : '/api/instructions/global'
      const { data, error } = await useMyFetch<Instruction>(endpoint, { method: 'POST', body })
      if (error.value) throw new Error((error.value as any)?.message || 'Create failed')
      toast.add({ title: 'Created', color: 'green' })
      creating.value = false; editing.value = false; createRefs.value = []
      await fetchAll()
      const created = (data.value as any)?.id ? allInstructions.value.find(i => i.id === (data.value as any).id) : null
      if (created) openInstruction(created)
    } else if (detail.value) {
      const { data, error } = await useMyFetch<Instruction>(`/api/instructions/${detail.value.id}`, { method: 'PUT', body })
      if (error.value) throw new Error((error.value as any)?.message || 'Save failed')
      toast.add({ title: 'Saved', color: 'green' }); editing.value = false
      if (data.value) detail.value = { ...detail.value, ...data.value }
      await fetchAll()
      const fresh = allInstructions.value.find(i => i.id === detail.value?.id)
      if (fresh) { detail.value = fresh; syncDraft(fresh) }
      loadVersions(detail.value!.id)
    }
  } catch (e: any) { toast.add({ title: 'Error', description: e.message, color: 'red' }) } finally { saving.value = false }
}

// ── Versions ────────────────────────────────────────────
const loadVersions = async (id: string) => {
  versionsLoading.value = true; versions.value = []
  try { const { data } = await useMyFetch<any>(`/api/instructions/${id}/versions`, { method: 'GET', query: { limit: 50 } }); versions.value = data.value?.items || [] } catch (e) {} finally { versionsLoading.value = false }
}
const restore = async (v: any) => {
  if (!detail.value) return
  if (!window.confirm(`Restore version v${v.version_number}? This creates a new version.`)) return
  try { await useMyFetch(`/api/instructions/${detail.value.id}/versions/${v.id}/revert`, { method: 'POST' }); toast.add({ title: `Restored v${v.version_number}`, color: 'green' }); await fetchAll(); const fresh = allInstructions.value.find(i => i.id === detail.value?.id); if (fresh) openInstruction(fresh) } catch (e: any) { toast.add({ title: 'Error', description: e?.message, color: 'red' }) }
}

// ── Display helpers ─────────────────────────────────────
const displayTitle = (ins: Instruction) => ins?.title || (ins?.text || '').split('\n')[0].slice(0, 60) || 'Untitled'
const refLabel = (ref: any) => ref.display_text || ref.object?.name || ref.object_type
const fmtDate = (s?: string) => { if (!s) return ''; try { return new Date(s).toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) } catch { return s } }

// ── Inline tree sub-components ──────────────────────────
const TreeGroup = defineComponent({
  props: { label: String, icon: String, count: { type: Number, default: undefined }, countAccent: Boolean, pending: Boolean, open: Boolean, mono: Boolean, indent: { type: Number, default: 0 }, addable: Boolean, badge: String, disabled: Boolean },
  emits: ['toggle', 'add', 'badge'],
  setup(props, { slots, emit }) {
    return () => createElement('div', {}, [
      createElement('div', {
        class: ['group w-full flex items-center gap-1.5 h-7 rounded-md text-xs text-gray-600 transition-colors min-w-0', props.disabled ? 'opacity-90' : 'hover:bg-gray-100 cursor-pointer'],
        style: { paddingLeft: (6 + props.indent * 14) + 'px', paddingRight: '8px' },
        onClick: () => { if (!props.disabled) emit('toggle') },
      }, [
        createElement(resolveComponent('UIcon'), { name: 'i-heroicons-chevron-right', class: ['w-3 h-3 transition-transform shrink-0', props.disabled ? 'text-gray-200' : 'text-gray-300', props.open ? 'rotate-90' : ''] }),
        slots.icon ? slots.icon() : (props.icon ? createElement(resolveComponent('UIcon'), { name: props.icon, class: 'w-4 h-4 text-gray-400 shrink-0' }) : null),
        createElement('span', { class: ['flex-1 text-left truncate', props.mono ? 'font-mono text-[11px]' : ''] }, props.label),
        props.badge ? createElement('button', { class: 'shrink-0 inline-flex items-center gap-0.5 px-1.5 h-5 rounded bg-blue-50 text-blue-600 text-[10px] font-medium hover:bg-blue-100', onClick: (e: Event) => { e.stopPropagation(); emit('badge') } }, [createElement(resolveComponent('UIcon'), { name: 'i-heroicons-key', class: 'w-2.5 h-2.5' }), props.badge]) : null,
        (props.addable && !props.disabled) ? createElement('button', { class: 'shrink-0 w-4 h-4 rounded hover:bg-gray-200 text-gray-400 opacity-0 group-hover:opacity-100 flex items-center justify-center', title: 'Add instruction', onClick: (e: Event) => { e.stopPropagation(); emit('add') } }, [createElement(resolveComponent('UIcon'), { name: 'i-heroicons-plus', class: 'w-3 h-3' })]) : null,
        (props.count !== undefined && !props.badge) ? createElement('span', { class: ['text-[11px] tabular-nums shrink-0', props.countAccent ? 'text-amber-600 font-medium' : 'text-gray-400'] }, String(props.count)) : null,
      ]),
      (props.open && !props.disabled) ? createElement('div', { class: 'space-y-0.5 mt-0.5' }, slots.default ? slots.default() : []) : null,
    ])
  },
})

const InstrLeaf = defineComponent({
  props: { ins: { type: Object as () => Instruction, required: true }, indent: { type: Number, default: 0 } },
  setup(props) {
    return () => {
      const ins = props.ins
      const sel = selectedId.value === ins.id
      return createElement('button', {
        class: ['group w-full flex items-center gap-2 h-7 rounded-md text-xs transition-colors min-w-0', sel ? 'bg-gray-100 text-gray-900' : 'text-gray-600 hover:bg-gray-100'],
        style: { paddingLeft: (20 + props.indent * 14) + 'px', paddingRight: '8px' },
        onClick: () => openInstruction(ins),
      }, [
        createElement('span', { class: ['shrink-0 w-1.5 h-1.5 rounded-full', h.getStatusIconClass(ins)], title: h.getStatusTooltip(ins) }),
        createElement('span', { class: 'flex-1 text-left truncate' }, displayTitle(ins)),
        createElement('span', { class: 'shrink-0 hidden xl:inline-flex items-center px-1.5 h-4 rounded bg-gray-50 text-gray-400 text-[10px] font-medium' }, h.formatCategory(ins.category)),
        createElement(resolveComponent('UIcon'), { name: h.getSourceIcon(ins), class: 'w-3 h-3 text-gray-300 shrink-0', title: h.getSourceTooltip(ins) }),
        createElement('span', { class: 'shrink-0 inline-flex items-center px-1.5 h-4 rounded bg-gray-100 text-gray-500 text-[10px] font-medium' }, h.getLoadModeLabel(ins.load_mode)),
        (ins.data_sources && ins.data_sources.length > 1) ? createElement('span', { class: 'shrink-0 inline-flex items-center px-1 h-4 rounded bg-gray-100 text-gray-500 text-[10px] font-medium', title: ins.data_sources.map(d => d.name).join(', ') }, String(ins.data_sources.length)) : null,
      ])
    }
  },
})

const EmptyHint = defineComponent({
  props: { text: String, add: Boolean, pad: { type: Number, default: 34 } },
  emits: ['add'],
  setup(props, { emit }) {
    return () => createElement('div', { class: 'flex items-center gap-2 py-1', style: { paddingLeft: props.pad + 'px' } }, [
      createElement('span', { class: 'text-[11px] text-gray-300 italic' }, props.text),
      props.add ? createElement('button', { class: 'text-[11px] text-gray-500 hover:text-gray-900 font-medium', onClick: (e: Event) => { e.stopPropagation(); emit('add') } }, '+ Add') : null,
    ])
  },
})

const FilterSection = defineComponent({
  props: { label: String, options: { type: Array as () => { value: string; label: string }[], default: () => [] }, modelValue: { type: Array as () => string[], default: () => [] } },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    const toggle = (v: string) => { const cur = [...(props.modelValue || [])]; const i = cur.indexOf(v); i >= 0 ? cur.splice(i, 1) : cur.push(v); emit('update:modelValue', cur) }
    return () => createElement('div', {}, [
      createElement('div', { class: 'text-[10px] font-semibold uppercase tracking-wider text-gray-400 mb-1' }, props.label),
      createElement('div', { class: 'flex flex-wrap gap-1' }, props.options.map(o => createElement('button', { key: o.value, type: 'button', class: ['px-2 h-6 rounded-md text-[11px] font-medium transition-colors', (props.modelValue || []).includes(o.value) ? 'bg-gray-900 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'], onClick: () => toggle(o.value) }, o.label))),
    ])
  },
})

onMounted(async () => { await Promise.all([fetchAgents(), fetchAll(), fetchLabels(), fetchCategories(), fetchGitStatus()]) })
</script>

<style scoped>
.prose-instruction :deep(.tiptap-prose) { min-height: 80px; }
</style>
