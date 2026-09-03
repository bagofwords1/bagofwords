<template>
  <div ref="root" class="kg relative w-full h-full min-h-[320px] select-none overflow-hidden rounded-xl" :class="isDark ? 'bg-gray-950' : 'bg-[#fafafa]'">
    <canvas ref="canvas" class="absolute inset-0 w-full h-full" :class="dragging ? 'cursor-grabbing' : (hover ? 'cursor-pointer' : 'cursor-grab')"
            @mousedown="onDown" @mousemove="onMove" @mouseup="onUp" @mouseleave="onLeave" @wheel.prevent="onWheel" @dblclick="onDbl" />

    <!-- Header: title + stats -->
    <div class="absolute top-3 start-3 pointer-events-none">
      <div class="pointer-events-auto rounded-lg bg-white/85 dark:bg-gray-900/85 backdrop-blur-sm ring-1 ring-gray-200/70 dark:ring-gray-800 shadow-sm px-3 py-2">
        <div class="flex items-center gap-1.5 text-[12px] font-medium text-gray-800 dark:text-gray-100">
          <UIcon name="i-heroicons-share" class="w-3.5 h-3.5 text-gray-400" />
          {{ scope === 'org' ? $t('knowledgeGraph.titleOrg') : $t('knowledgeGraph.titleAgent') }}
        </div>
        <div v-if="stats" class="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-gray-500 dark:text-gray-400">
          <template v-if="scope === 'org'">
            <span><b class="text-gray-800 dark:text-gray-200 font-medium">{{ stats.agents }}</b> {{ $t('knowledgeGraph.agents') }}</span>
            <span><b class="text-gray-800 dark:text-gray-200 font-medium">{{ stats.instructions }}</b> {{ $t('knowledgeGraph.instructions') }}</span>
            <span><b class="text-gray-800 dark:text-gray-200 font-medium">{{ stats.shared }}</b> {{ $t('knowledgeGraph.shared') }}</span>
            <span v-if="stats.global"><b class="text-gray-800 dark:text-gray-200 font-medium">{{ stats.global }}</b> {{ $t('knowledgeGraph.global') }}</span>
            <span v-if="isolatedCount"><b class="text-gray-800 dark:text-gray-200 font-medium">{{ isolatedCount }}</b> {{ $t('knowledgeGraph.unconnected') }}</span>
          </template>
          <template v-else>
            <span><b class="text-gray-800 dark:text-gray-200 font-medium">{{ stats.tables }}</b> {{ $t('knowledgeGraph.tables') }}</span>
            <span><b class="text-gray-800 dark:text-gray-200 font-medium">{{ stats.fks }}</b> {{ $t('knowledgeGraph.joins') }}</span>
            <span><b class="text-gray-800 dark:text-gray-200 font-medium">{{ stats.instructions }}</b> {{ $t('knowledgeGraph.instructions') }}</span>
          </template>
        </div>
      </div>
    </div>

    <!-- Controls -->
    <div class="absolute top-3 end-3 flex items-center gap-1.5">
      <div class="relative">
        <UIcon name="i-heroicons-magnifying-glass" class="w-3.5 h-3.5 text-gray-400 absolute start-2 top-1/2 -translate-y-1/2 pointer-events-none" />
        <input v-model="query" type="text" :placeholder="$t('knowledgeGraph.search')"
               class="h-7 w-44 ps-7 pe-2 rounded-md text-[11px] bg-white/90 dark:bg-gray-900/90 backdrop-blur-sm ring-1 ring-gray-200/80 dark:ring-gray-800 text-gray-700 dark:text-gray-200 placeholder:text-gray-400 outline-none focus:ring-blue-400" />
      </div>
      <button type="button" class="kg-btn" :class="showLabels && 'kg-btn-on'" :title="$t('knowledgeGraph.toggleLabels')" @click="showLabels = !showLabels"><UIcon name="i-heroicons-tag" class="w-3.5 h-3.5" /></button>
      <button v-if="scope === 'org'" type="button" class="kg-btn" :class="showGlobal && 'kg-btn-on'" :title="$t('knowledgeGraph.toggleGlobal')" @click="showGlobal = !showGlobal"><UIcon name="i-heroicons-globe-alt" class="w-3.5 h-3.5" /></button>
      <button v-if="scope === 'org'" type="button" class="kg-btn" :class="showIsolated && 'kg-btn-on'" :title="$t('knowledgeGraph.toggleUnconnected')" @click="showIsolated = !showIsolated"><UIcon name="i-heroicons-minus-circle" class="w-3.5 h-3.5" /></button>
      <button type="button" class="kg-btn" :class="showTitles && 'kg-btn-on'" :title="$t('knowledgeGraph.toggleTitles')" @click="showTitles = !showTitles"><UIcon name="i-heroicons-language" class="w-3.5 h-3.5" /></button>
      <button type="button" class="kg-btn" :title="$t('knowledgeGraph.fit')" @click="fit(true)"><UIcon name="i-heroicons-arrows-pointing-in" class="w-3.5 h-3.5" /></button>
    </div>

    <!-- Legend -->
    <div v-if="legend.length" class="absolute bottom-3 start-3 max-w-[62%] rounded-lg bg-white/85 dark:bg-gray-900/85 backdrop-blur-sm ring-1 ring-gray-200/70 dark:ring-gray-800 shadow-sm px-2.5 py-2">
      <div class="text-[10px] uppercase tracking-wider font-semibold text-gray-400 dark:text-gray-500 mb-1">{{ scope === 'org' ? $t('knowledgeGraph.topics') : $t('knowledgeGraph.legend') }}</div>
      <div class="flex flex-wrap gap-x-2.5 gap-y-1">
        <button v-for="l in legend" :key="l.id" type="button" class="inline-flex items-center gap-1 text-[11px] rounded px-1 -mx-1 transition-colors"
                :class="focusCommunity === l.id ? 'bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-white' : 'text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white'"
                @click="focusCommunity = focusCommunity === l.id ? null : l.id">
          <span class="w-2 h-2 rounded-full shrink-0" :style="{ background: l.color }" />{{ l.name }}<span v-if="l.count" class="text-gray-400 dark:text-gray-500">{{ l.count }}</span>
        </button>
      </div>
    </div>

    <div v-if="!selected" class="absolute bottom-3 end-3 text-[10px] text-gray-400 dark:text-gray-500 pointer-events-none">{{ $t('knowledgeGraph.hint') }}</div>

    <!-- Selected node card -->
    <div v-if="selected" class="absolute bottom-3 end-3 w-64 rounded-lg bg-white/95 dark:bg-gray-900/95 backdrop-blur-sm ring-1 ring-gray-200/70 dark:ring-gray-800 shadow-md px-3 py-2.5">
      <div class="flex items-start justify-between gap-2">
        <div class="min-w-0">
          <div class="text-[10px] uppercase tracking-wider font-semibold" :style="{ color: selected.color }">{{ kindLabel(selected) }}</div>
          <div class="text-[12px] font-medium text-gray-900 dark:text-white truncate" :title="selected.label">{{ selected.label }}</div>
        </div>
        <button class="h-6 w-6 rounded-md flex items-center justify-center text-gray-300 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 shrink-0" @click="selected = null"><UIcon name="i-heroicons-x-mark" class="w-3.5 h-3.5" /></button>
      </div>
      <div class="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-gray-500 dark:text-gray-400">
        <span v-if="selected.kind === 'agent'">{{ $t('knowledgeGraph.nInstructions', { n: degreeOf(selected, 'instruction') }) }}</span>
        <span v-if="selected.kind === 'agent' && selected.communityName">{{ selected.communityName }}</span>
        <span v-if="selected.kind === 'instruction'">{{ selected.is_global ? $t('knowledgeGraph.globalInstruction') : $t('knowledgeGraph.nAgents', { n: selected.agent_count }) }}</span>
        <span v-if="selected.kind === 'instruction' && selected.load_mode" class="capitalize">{{ selected.load_mode }}</span>
        <span v-if="selected.kind === 'table'">{{ $t('knowledgeGraph.nColumns', { n: selected.columns }) }}</span>
        <span v-if="selected.kind === 'table'">{{ $t('knowledgeGraph.nReferences', { n: selected.ref_count }) }}</span>
        <span v-if="selected.kind === 'label'">{{ $t('knowledgeGraph.nInstructions', { n: degreeOf(selected, 'instruction') }) }}</span>
      </div>
      <div v-if="neighborsOf(selected).length" class="mt-2 max-h-24 overflow-y-auto space-y-0.5">
        <button v-for="n in neighborsOf(selected).slice(0, 12)" :key="n.id" type="button" class="w-full text-start text-[11px] truncate text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white flex items-center gap-1.5" @click="select(n)">
          <span class="w-1.5 h-1.5 rounded-full shrink-0" :style="{ background: n.color }" />{{ n.label }}
        </button>
      </div>
      <button v-if="selected.kind !== 'label'" type="button" class="mt-2 h-7 w-full rounded-md bg-blue-600 text-white text-[11px] font-medium hover:bg-blue-700 inline-flex items-center justify-center gap-1" @click="open(selected)">
        <UIcon name="i-heroicons-arrow-top-right-on-square" class="w-3 h-3" />{{ $t('knowledgeGraph.open') }}
      </button>
    </div>

    <!-- Tooltip -->
    <div v-if="hover && !dragging" class="absolute pointer-events-none rounded-md bg-gray-900/90 dark:bg-white/95 text-white dark:text-gray-900 text-[11px] px-2 py-1 shadow-lg max-w-[240px]"
         :style="{ left: tip.x + 'px', top: tip.y + 'px', transform: 'translate(12px, -50%)' }">
      <div class="font-medium truncate">{{ hover.label }}</div>
      <div class="opacity-70">{{ kindLabel(hover) }}<template v-if="hover.kind === 'instruction' && hover.agent_count > 1"> · {{ $t('knowledgeGraph.sharedBy', { n: hover.agent_count }) }}</template><template v-if="hover.kind === 'agent'"> · {{ $t('knowledgeGraph.nInstructions', { n: degreeOf(hover, 'instruction') }) }}</template></div>
    </div>

    <div v-if="loading" class="absolute inset-0 flex items-center justify-center text-[12px] text-gray-400"><UIcon name="i-heroicons-arrow-path" class="w-4 h-4 animate-spin me-1.5" />{{ $t('knowledgeGraph.loading') }}</div>
    <div v-else-if="!allNodes.length" class="absolute inset-0 flex items-center justify-center text-[12px] text-gray-400">{{ $t('knowledgeGraph.empty') }}</div>
  </div>
</template>

<script setup lang="ts">
import { forceSimulation, forceLink, forceManyBody, forceCollide, forceCenter, forceX, forceY, forceRadial } from 'd3-force'

type Kind = 'agent' | 'instruction' | 'table' | 'label'
interface GNode { id: string; kind: Kind; label: string; x: number; y: number; vx?: number; vy?: number; fx?: number | null; fy?: number | null; r: number; color: string; community?: string; communityName?: string; isolated?: boolean; hub?: boolean; [k: string]: any }
interface GEdge { source: any; target: any; kind: string; label?: string; color?: string }

const props = withDefaults(defineProps<{ scope: 'org' | 'agent'; agentId?: string | null; showAll?: boolean }>(), { agentId: null, showAll: false })
const emit = defineEmits<{ (e: 'open-agent', id: string): void; (e: 'open-instruction', id: string): void; (e: 'open-table', agentId: string, tableId: string): void }>()
const { t } = useI18n()

const PALETTE = ['#3b82f6', '#f59e0b', '#10b981', '#8b5cf6', '#ef4444', '#06b6d4', '#ec4899', '#84cc16', '#f97316', '#14b8a6', '#6366f1', '#a855f7', '#0ea5e9', '#d946ef']
const SOLO = '#9ca3af'

const root = ref<HTMLElement | null>(null)
const canvas = ref<HTMLCanvasElement | null>(null)
const loading = ref(false)
const stats = ref<any>(null)
const allNodes = ref<GNode[]>([])          // everything the API returned
const allEdges = ref<{ source: string; target: string; kind: string; label?: string; color?: string }[]>([])
const legend = ref<{ id: string; name: string; color: string; count?: number }[]>([])
const query = ref('')
const showLabels = ref(props.scope === 'agent')
const showGlobal = ref(false)
const showIsolated = ref(true)
const showTitles = ref(false)
const focusCommunity = ref<string | null>(null)
const selected = ref<GNode | null>(null)
const hover = ref<GNode | null>(null)
const dragging = ref(false)
const tip = reactive({ x: 0, y: 0 })
const isDark = ref(false)
const isolatedCount = ref(0)

// live simulation state (subset of allNodes/allEdges)
let simNodes: GNode[] = []
let simEdges: GEdge[] = []
let ringNodes: GNode[] = []
let sim: any = null
let tf = { x: 0, y: 0, k: 1 }
let ctx: CanvasRenderingContext2D | null = null
let raf = 0
let W = 0, H = 0, DPR = 1
let dragNode: GNode | null = null
let userMoved = false   // once the user pans/zooms, stop auto-refitting on resize
let panStart: { x: number; y: number; tx: number; ty: number } | null = null
let downAt = { x: 0, y: 0, moved: false }
const icons = new Map<string, HTMLImageElement | null>()
let adjacency = new Map<string, Set<string>>()
let nodeById = new Map<string, GNode>()

// ---------------------------------------------------------------- data ------
const fetchGraph = async () => {
  loading.value = true; selected.value = null; hover.value = null; userMoved = false
  try {
    const q: Record<string, any> = {}
    if (props.scope === 'agent' && props.agentId) q.data_source_id = props.agentId
    if (props.showAll) q.show_all = true
    const { data } = await useMyFetch<any>('/knowledge_graph', { method: 'GET', query: q })
    build(data.value || { nodes: [], edges: [], stats: null })
  } catch (e) { console.error(e); build({ nodes: [], edges: [], stats: null }) } finally { loading.value = false }
}

const normType = (raw?: string | null) => (raw || '').toLowerCase().replace(/-\d+$/, '')
const iconFor = (n: GNode): HTMLImageElement | null => {
  const key = normType(n.connector_key || n.type)
  if (!key) return null
  if (!icons.has(key)) {
    const img = new Image()
    img.onload = () => { icons.set(key, img); draw() }
    img.onerror = () => { icons.set(key, null) }
    img.src = `/data_sources_icons/${key}.png`
    icons.set(key, null)
  }
  return icons.get(key) || null
}

const hex = (h: string) => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16))
const mix = (a: string, b: string, t: number) => { const pa = hex(a), pb = hex(b); const c = pa.map((v, i) => Math.round(v + (pb[i] - v) * t)); return `rgb(${c[0]},${c[1]},${c[2]})` }
const rgba = (color: string, a: number) => color.startsWith('#') ? (([r, g, b]) => `rgba(${r},${g},${b},${a})`)(hex(color)) : color.replace(/^rgb\((.*)\)$/, `rgba($1,${a})`)
const TW: Record<string, string> = { blue: '#3b82f6', amber: '#f59e0b', gray: '#6b7280', red: '#ef4444', violet: '#8b5cf6', emerald: '#10b981', rose: '#f43f5e', sky: '#0ea5e9', indigo: '#6366f1', teal: '#14b8a6', green: '#22c55e', yellow: '#eab308', orange: '#f97316', pink: '#ec4899', purple: '#a855f7', cyan: '#06b6d4', lime: '#84cc16', fuchsia: '#d946ef' }
const labelColor = (c?: string | null) => (!c ? '#f59e0b' : c.startsWith('#') ? c : (TW[c] || '#f59e0b'))

/**
 * Topics from structure (Obsidian-style, no text analysis): project the
 * bipartite agent/instruction graph onto agents (two agents are linked when
 * they share an instruction), run weighted label propagation on that, then
 * hand each instruction the community of its agents. Agents that share nothing
 * stay "standalone" and are drawn muted so real clusters stand out.
 */
const detectCommunities = (ns: GNode[], es: { source: string; target: string; kind: string }[]) => {
  const agents = ns.filter(n => n.kind === 'agent')
  const insAgents = new Map<string, string[]>()
  for (const e of es) {
    if (e.kind !== 'attached') continue
    const [a, i] = e.source.startsWith('agent:') ? [e.source, e.target] : [e.target, e.source]
    if (!insAgents.has(i)) insAgents.set(i, []); insAgents.get(i)!.push(a)
  }
  const w = new Map<string, Map<string, number>>()
  for (const a of agents) w.set(a.id, new Map())
  for (const as of insAgents.values()) for (const a of as) for (const b of as) if (a !== b && w.has(a) && w.has(b)) w.get(a)!.set(b, (w.get(a)!.get(b) || 0) + 1)
  const lab = new Map<string, string>(agents.map(a => [a.id, a.id]))
  const order = [...agents].sort((a, b) => (w.get(b.id)!.size - w.get(a.id)!.size) || a.id.localeCompare(b.id))
  for (let it = 0; it < 30; it++) {
    let changed = 0
    for (const a of order) {
      const nb = w.get(a.id)!; if (!nb.size) continue
      const score = new Map<string, number>()
      for (const [b, wt] of nb) { const l = lab.get(b)!; score.set(l, (score.get(l) || 0) + wt) }
      score.set(lab.get(a.id)!, (score.get(lab.get(a.id)!) || 0) + 0.5)  // inertia
      let best = lab.get(a.id)!, bs = -1
      for (const [l, sc] of score) if (sc > bs || (sc === bs && l < best)) { best = l; bs = sc }
      if (best !== lab.get(a.id)) { lab.set(a.id, best); changed++ }
    }
    if (!changed) break
  }
  const groups = new Map<string, GNode[]>()
  for (const a of agents) { if (!w.get(a.id)!.size) continue; const l = lab.get(a.id)!; if (!groups.has(l)) groups.set(l, []); groups.get(l)!.push(a) }
  const nameFor = (members: GNode[]) => {
    const counts = new Map<string, number>()
    for (const m of members) { const head = m.label.split(/[·:|\/]|\s[-–—]\s/)[0].trim(); counts.set(head, (counts.get(head) || 0) + 1) }
    let best = '', bc = 0; for (const [k, c] of counts) if (c > bc) { best = k; bc = c }
    return best || members[0]?.label || ''
  }
  // merge communities that resolve to the same name (small fragments of one domain)
  const byName = new Map<string, GNode[]>()
  for (const members of groups.values()) { const nm = nameFor(members); if (!byName.has(nm)) byName.set(nm, []); byName.get(nm)!.push(...members) }
  const ranked = [...byName.entries()].sort((a, b) => b[1].length - a[1].length)
  const leg: { id: string; name: string; color: string; count: number }[] = []
  const bg = isDark.value ? '#111827' : '#ffffff'
  ranked.forEach(([name, members], i) => {
    const top = i < 10
    const id = top ? `c${i}` : 'other', color = top ? PALETTE[i % PALETTE.length] : SOLO
    for (const m of members) { m.community = id; m.communityName = top ? name : t('knowledgeGraph.other'); m.color = color }
    if (top) leg.push({ id, name, color, count: members.length })
    else { const o = leg.find(l => l.id === 'other'); if (o) o.count += members.length; else leg.push({ id: 'other', name: t('knowledgeGraph.other'), color, count: members.length }) }
  })
  let solo = 0
  for (const a of agents) if (!a.community) { a.community = 'solo'; a.communityName = t('knowledgeGraph.standalone'); a.color = SOLO; solo++ }
  if (solo) leg.push({ id: 'solo', name: t('knowledgeGraph.standalone'), color: SOLO, count: solo })
  // instructions inherit the majority community of their agents
  for (const n of ns) {
    if (n.kind !== 'instruction') continue
    if (n.is_global) { n.community = 'global'; n.color = SOLO; continue }
    const counts = new Map<string, number>()
    for (const a of insAgents.get(n.id) || []) { const c = nodeById.get(a)?.community || 'solo'; counts.set(c, (counts.get(c) || 0) + 1) }
    let best = 'solo', bc = 0; for (const [c, k] of counts) if (k > bc) { best = c; bc = k }
    n.community = best
    const base = best === 'solo' || best === 'other' ? SOLO : (leg.find(l => l.id === best)?.color || SOLO)
    n.color = mix(base, bg, best === 'solo' ? 0.35 : 0.42)
  }
  return leg
}

const build = (data: any) => {
  stats.value = data.stats
  const ns: GNode[] = (data.nodes || []).map((n: any) => ({ ...n, x: (Math.random() - 0.5) * 800, y: (Math.random() - 0.5) * 800, r: 4, color: SOLO }))
  nodeById = new Map(ns.map(n => [n.id, n]))
  const es = (data.edges || []).filter((e: any) => nodeById.has(e.source) && nodeById.has(e.target))
  adjacency = new Map()
  const deg = new Map<string, number>()
  for (const e of es) {
    deg.set(e.source, (deg.get(e.source) || 0) + 1); deg.set(e.target, (deg.get(e.target) || 0) + 1)
    if (!adjacency.has(e.source)) adjacency.set(e.source, new Set()); if (!adjacency.has(e.target)) adjacency.set(e.target, new Set())
    adjacency.get(e.source)!.add(e.target); adjacency.get(e.target)!.add(e.source)
  }
  for (const n of ns) {
    const d = deg.get(n.id) || 0
    if (n.kind === 'agent') n.r = props.scope === 'agent' ? 24 : 7 + Math.min(11, Math.sqrt(d) * 2.6)
    else if (n.kind === 'instruction') n.r = n.is_global ? 4.5 : 3.2 + Math.min(4.5, Math.max(0, (n.agent_count || 1) - 1) * 1.2)
    else if (n.kind === 'label') n.r = 7 + Math.min(8, Math.sqrt(d) * 1.2)
    else if (n.kind === 'table') n.r = 8 + Math.min(10, (n.ref_count || 0) * 1.6 + (n.centrality || 0) * 3)
  }
  if (props.scope === 'org') {
    legend.value = detectCommunities(ns, es)
    // hubs: the agents whose names are worth showing at overview zoom
    const ag = ns.filter(n => n.kind === 'agent').sort((a, b) => (deg.get(b.id) || 0) - (deg.get(a.id) || 0))
    ag.slice(0, Math.max(8, Math.ceil(ag.length * 0.12))).forEach(a => { a.hub = true })
  } else {
    for (const n of ns) {
      if (n.kind === 'table') n.color = isDark.value ? '#94a3b8' : '#475569'
      if (n.kind === 'instruction') n.color = n.is_global ? SOLO : n.shared ? '#8b5cf6' : '#3b82f6'
      if (n.kind === 'agent') { n.color = isDark.value ? '#e5e7eb' : '#111827'; n.fx = 0; n.fy = 0 }
    }
    legend.value = [
      { id: 'table', name: t('knowledgeGraph.tables'), color: isDark.value ? '#94a3b8' : '#475569' },
      { id: 'instruction', name: t('knowledgeGraph.instructions'), color: '#3b82f6' },
      { id: 'shared', name: t('knowledgeGraph.sharedInstructions'), color: '#8b5cf6' },
      { id: 'label', name: t('knowledgeGraph.labels'), color: '#f59e0b' },
    ]
  }
  for (const n of ns) if (n.kind === 'label') n.color = labelColor(n.color)
  for (const e of es) {
    const s = nodeById.get(e.source)!, tg = nodeById.get(e.target)!
    if (e.kind === 'labeled') e.color = tg.color
    else if (e.kind === 'fk') e.color = isDark.value ? '#64748b' : '#94a3b8'
    else if (e.kind === 'references') e.color = s.kind === 'instruction' ? s.color : tg.color
    else e.color = (s.kind === 'agent' ? s : tg.kind === 'agent' ? tg : s).color
  }
  allNodes.value = ns; allEdges.value = es
  rebuild()
}

// ----------------------------------------------------------- simulation ----
const passes = (n: GNode) =>
  (n.kind !== 'label' || showLabels.value) &&
  (n.kind !== 'instruction' || !n.is_global || showGlobal.value || props.scope === 'agent')

const rebuild = () => {
  if (sim) sim.stop()
  const ns = allNodes.value.filter(passes)
  const idset = new Set(ns.map(n => n.id))
  const es: GEdge[] = allEdges.value.filter(e => idset.has(e.source) && idset.has(e.target)).map(e => ({ ...e }))
  const linked = new Set<string>()
  for (const e of es) { linked.add(e.source as string); linked.add(e.target as string) }
  for (const n of ns) n.isolated = !linked.has(n.id) && n.kind === 'agent'
  simNodes = ns.filter(n => !n.isolated)
  ringNodes = ns.filter(n => n.isolated)
  isolatedCount.value = ringNodes.length
  simEdges = es
  const agentScope = props.scope === 'agent'
  sim = forceSimulation(simNodes as any)
    .force('link', forceLink(simEdges as any).id((d: any) => d.id)
      .distance((e: any) => e.kind === 'labeled' ? 90 : e.kind === 'fk' ? 120 : e.kind === 'references' ? 110 : (agentScope ? 230 : 30))
      .strength((e: any) => e.kind === 'labeled' ? (agentScope ? 0.05 : 0.02) : e.kind === 'fk' ? 0.3 : e.kind === 'references' ? 0.45 : (agentScope ? 0.2 : 0.85)))
    .force('charge', forceManyBody().strength((d: any) => d.kind === 'agent' ? (agentScope ? -600 : -260) : d.kind === 'label' ? (agentScope ? -160 : -120) : d.kind === 'table' ? -500 : (agentScope ? -320 : -26)).distanceMax(agentScope ? 700 : 520))
    .force('collide', forceCollide().radius((d: any) => d.r + (agentScope ? (d.kind === 'agent' ? 30 : d.kind === 'table' ? 36 : d.kind === 'label' ? 30 : 38) : (d.kind === 'agent' ? 8 : 3))).strength(0.95).iterations(3))
    // Agent scope is hub-and-spoke: agent in the middle, its tables on an inner
    // ring, instructions around the tables they reference, label hubs outermost.
    .force('radial', agentScope ? forceRadial((d: any) => d.kind === 'agent' ? 0 : d.kind === 'table' ? 105 : d.kind === 'instruction' ? 205 : 285, 0, 0).strength((d: any) => d.kind === 'label' ? 0.9 : 0.6) : null)
    .force('center', forceCenter(0, 0))
    .force('x', forceX(0).strength(agentScope ? 0 : 0.03))
    .force('y', forceY(0).strength(agentScope ? 0 : 0.03))
    .stop()
  // Settle synchronously: a finished layout on the first frame beats watching it wobble.
  const ticks = simNodes.length > 1200 ? 220 : 360
  for (let i = 0; i < ticks; i++) sim.tick()
  sim.on('tick', draw)
  placeRing()
  fit(false)
  // Dev-only hook so browser tests can locate nodes on the canvas.
  if (import.meta.dev) (window as any).__kg = { nodes: () => drawn(), tf: () => tf }
}

// Agents with no instructions form a quiet ring around the connected graph:
// visible (they are real), but not fighting the layout for space.
const placeRing = () => {
  if (!ringNodes.length) return
  let R = 0
  for (const n of simNodes) R = Math.max(R, Math.hypot(n.x, n.y) + n.r)
  const ringR = Math.max(R + 55, ringNodes.length * 13 / (2 * Math.PI))
  ringNodes.forEach((n, i) => { const a = (i / ringNodes.length) * Math.PI * 2 - Math.PI / 2; n.x = Math.cos(a) * ringR; n.y = Math.sin(a) * ringR })
}

// ----------------------------------------------------------------- view ----
const resize = () => {
  const el = root.value, c = canvas.value
  if (!el || !c) return
  const rect = el.getBoundingClientRect()
  DPR = window.devicePixelRatio || 1
  W = Math.max(1, Math.floor(rect.width)); H = Math.max(1, Math.floor(rect.height))
  c.width = W * DPR; c.height = H * DPR
  ctx = c.getContext('2d')
  draw()
}
const drawn = () => [...simNodes, ...(showIsolated.value ? ringNodes : [])]
const fit = (animate = false) => {
  const vs = drawn()
  if (!vs.length || !W || !H) return
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity
  for (const n of vs) { x0 = Math.min(x0, n.x - n.r); y0 = Math.min(y0, n.y - n.r); x1 = Math.max(x1, n.x + n.r); y1 = Math.max(y1, n.y + n.r) }
  const padX = 40, padTop = 72, padBottom = 96
  const k = Math.max(0.1, Math.min(3, Math.min((W - padX * 2) / Math.max(1, x1 - x0), (H - padTop - padBottom) / Math.max(1, y1 - y0))))
  const target = { k, x: W / 2 - ((x0 + x1) / 2) * k, y: (H + padTop - padBottom) / 2 - ((y0 + y1) / 2) * k }
  if (!animate) { tf = target; draw(); return }
  const from = { ...tf }; const t0 = performance.now()
  const step = (now: number) => {
    const p = Math.min(1, (now - t0) / 350), e = 1 - Math.pow(1 - p, 3)
    tf = { k: from.k + (target.k - from.k) * e, x: from.x + (target.x - from.x) * e, y: from.y + (target.y - from.y) * e }
    draw(); if (p < 1) requestAnimationFrame(step)
  }
  requestAnimationFrame(step)
}
const toGraph = (sx: number, sy: number) => ({ x: (sx - tf.x) / tf.k, y: (sy - tf.y) / tf.k })

// -------------------------------------------------------------- drawing ----
const highlightSet = computed<Set<string> | null>(() => {
  const q = query.value.trim().toLowerCase()
  const focus = hover.value || selected.value
  if (q) { const s = new Set<string>(); for (const n of allNodes.value) if (n.label.toLowerCase().includes(q)) s.add(n.id); return s }
  if (focus) { const s = new Set<string>([focus.id]); for (const m of adjacency.get(focus.id) || []) s.add(m); return s }
  if (focusCommunity.value) {
    const s = new Set<string>(); const f = focusCommunity.value
    for (const n of allNodes.value) if (n.community === f || (props.scope === 'agent' && (n.kind === f || (f === 'shared' && n.shared) || (f === 'instruction' && n.kind === 'instruction' && !n.shared)))) s.add(n.id)
    return s
  }
  return null
})

const draw = () => { if (raf) return; raf = requestAnimationFrame(() => { raf = 0; paint() }) }
const paint = () => {
  if (!ctx) return
  const c = ctx
  const hl = highlightSet.value
  const dark = isDark.value
  const bg = dark ? '#030712' : '#fafafa'
  const focusMode = !!(hover.value || selected.value)
  const q = !!query.value.trim()
  c.setTransform(DPR, 0, 0, DPR, 0, 0)
  c.clearRect(0, 0, W, H)
  c.fillStyle = dark ? 'rgba(255,255,255,0.05)' : 'rgba(0,0,0,0.045)'
  const gs = 28 * tf.k
  if (gs > 12) { const ox = ((tf.x % gs) + gs) % gs, oy = ((tf.y % gs) + gs) % gs; for (let x = ox; x < W; x += gs) for (let y = oy; y < H; y += gs) c.fillRect(x - 0.5, y - 0.5, 1, 1) }
  c.setTransform(DPR * tf.k, 0, 0, DPR * tf.k, DPR * tf.x, DPR * tf.y)
  const k = tf.k
  // edges
  c.lineCap = 'round'
  for (const e of simEdges) {
    const s = e.source as GNode, tg = e.target as GNode
    const on = hl ? (hl.has(s.id) && hl.has(tg.id)) : true
    const base = e.kind === 'labeled' ? (props.scope === 'agent' ? 0.22 : 0.05) : e.kind === 'fk' ? 0.6 : e.kind === 'references' ? 0.45 : 0.35
    const a = hl ? (on ? Math.min(1, base + 0.5) : 0.03) : base
    c.strokeStyle = rgba(e.color || SOLO, a)
    c.lineWidth = (on && hl ? 1.6 : 1) / k
    c.setLineDash(e.kind === 'fk' ? [4 / k, 3 / k] : [])
    c.beginPath(); c.moveTo(s.x, s.y); c.lineTo(tg.x, tg.y); c.stroke()
  }
  c.setLineDash([])
  const fontPx = 11 / k   // constant 11px on screen regardless of zoom
  const textColor = dark ? '#e5e7eb' : '#1f2937', mutedText = dark ? '#9ca3af' : '#6b7280'
  const drawText = (txt: string, x: number, y: number, color: string, mono = false, weight = 400, size = fontPx) => {
    c.font = `${weight} ${size}px ${mono ? 'ui-monospace, SFMono-Regular, Menlo, monospace' : 'ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif'}`
    c.textAlign = 'center'; c.textBaseline = 'top'
    c.lineWidth = 3 / Math.max(k, 0.5); c.strokeStyle = rgba(bg, 0.9); c.lineJoin = 'round'
    c.strokeText(txt, x, y); c.fillStyle = color; c.fillText(txt, x, y)
  }
  const order = drawn().sort((a, b) => rank(a) - rank(b))
  for (const n of order) {
    const on = hl ? hl.has(n.id) : true
    c.globalAlpha = hl ? (on ? 1 : 0.16) : (n.isolated ? 0.55 : 1)
    const isSel = selected.value?.id === n.id, isHov = hover.value?.id === n.id
    if (n.kind === 'agent') {
      c.beginPath(); c.arc(n.x, n.y, n.r + (isSel || isHov ? 3 : 0), 0, Math.PI * 2)
      c.fillStyle = rgba(n.color, isSel || isHov ? 0.35 : 0.18); c.fill()
      c.beginPath(); c.arc(n.x, n.y, n.r, 0, Math.PI * 2); c.fillStyle = dark ? '#111827' : '#ffffff'; c.fill()
      c.lineWidth = 2 / Math.max(k, 0.6); c.strokeStyle = n.color; c.stroke()
      const img = iconFor(n)
      if (img && n.r * k > 5) { const s = n.r * 1.1; c.save(); c.beginPath(); c.arc(n.x, n.y, n.r - 2 / k, 0, Math.PI * 2); c.clip(); c.drawImage(img, n.x - s / 2, n.y - s / 2, s, s); c.restore() }
      else { c.fillStyle = n.color; c.font = `600 ${n.r}px ui-sans-serif, system-ui, sans-serif`; c.textAlign = 'center'; c.textBaseline = 'middle'; c.fillText((n.label || '?')[0].toUpperCase(), n.x, n.y + n.r * 0.05) }
      const showName = props.scope === 'agent' || isSel || isHov || (hl && on && (focusMode || !!query.value)) || (!n.isolated && (n.hub || k > 1.5)) || (n.isolated && k > 2.2)
      if (showName) drawText(n.label, n.x, n.y + n.r + 3 / k, n.isolated ? mutedText : textColor, false, 500)
    } else if (n.kind === 'instruction') {
      c.beginPath(); c.arc(n.x, n.y, n.r + (isSel || isHov ? 2 : 0), 0, Math.PI * 2)
      if (n.is_global) { c.fillStyle = dark ? '#111827' : '#ffffff'; c.fill(); c.setLineDash([2 / k, 2 / k]); c.lineWidth = 1.2 / k; c.strokeStyle = mutedText; c.stroke(); c.setLineDash([]) }
      else { c.fillStyle = n.color; c.fill(); if (n.load_mode === 'disabled') { c.fillStyle = rgba(dark ? '#111827' : '#ffffff', 0.55); c.fill() } }
      if (isSel || isHov || (q && on)) { c.lineWidth = 1.5 / k; c.strokeStyle = textColor; c.stroke() }
      const showTitle = isSel || isHov || (hl && on && (focusMode || q)) || showTitles.value || k > 2.4 || props.scope === 'agent'
      if (showTitle) {
        const above = props.scope === 'agent' && n.y < 0
        drawText(n.label, n.x, above ? n.y - n.r - 2.5 / k - 10 / k : n.y + n.r + 2.5 / k, (hl && on) || props.scope === 'agent' ? textColor : mutedText, false, 400, 10 / k)
      }
    } else if (n.kind === 'label') {
      c.beginPath(); c.arc(n.x, n.y, n.r + (isSel || isHov ? 2 : 0), 0, Math.PI * 2)
      c.fillStyle = rgba(n.color, 0.16); c.fill(); c.lineWidth = 1.5 / k; c.strokeStyle = n.color; c.setLineDash([3 / k, 2 / k]); c.stroke(); c.setLineDash([])
      c.fillStyle = n.color; c.font = `700 ${Math.max(7, n.r * 0.9)}px ui-sans-serif, system-ui, sans-serif`; c.textAlign = 'center'; c.textBaseline = 'middle'; c.fillText('#', n.x, n.y + 0.5)
      drawText(n.label, n.x, n.y + n.r + 3 / k, n.color, false, 600)
    } else if (n.kind === 'table') {
      const s = n.r * 1.7, rr = 4
      c.beginPath(); c.roundRect(n.x - s / 2, n.y - s / 2, s, s, rr)
      c.fillStyle = dark ? '#1f2937' : '#ffffff'; c.fill(); c.lineWidth = (isSel || isHov ? 2 : 1.4) / k; c.strokeStyle = isSel || isHov ? textColor : n.color; c.stroke()
      c.strokeStyle = rgba(n.color, 0.7); c.lineWidth = 1 / k
      const gx = n.x - s / 2 + s * 0.22, gy = n.y - s / 2 + s * 0.28, gw = s * 0.56, gh = s * 0.44
      c.beginPath(); c.rect(gx, gy, gw, gh); c.moveTo(gx, gy + gh / 2); c.lineTo(gx + gw, gy + gh / 2); c.moveTo(gx + gw / 2, gy); c.lineTo(gx + gw / 2, gy + gh); c.stroke()
      drawText(n.label, n.x, n.y + s / 2 + 3 / k, textColor, true, 500)
    }
  }
  c.globalAlpha = 1
}
const rank = (n: GNode) => n.kind === 'instruction' ? 0 : n.kind === 'label' ? 1 : n.kind === 'table' ? 2 : 3

// ---------------------------------------------------------- interaction ----
const hit = (sx: number, sy: number): GNode | null => {
  const p = toGraph(sx, sy)
  let best: GNode | null = null, bd = Infinity
  for (const n of drawn()) {
    const dx = n.x - p.x, dy = n.y - p.y, d = Math.sqrt(dx * dx + dy * dy)
    const rr = n.r + 4 / tf.k
    if (d < rr && d < bd) { bd = d; best = n }
  }
  return best
}
const local = (ev: MouseEvent) => { const r = canvas.value!.getBoundingClientRect(); return { x: ev.clientX - r.left, y: ev.clientY - r.top } }
const onDown = (ev: MouseEvent) => {
  const p = local(ev); downAt = { x: p.x, y: p.y, moved: false }
  const n = hit(p.x, p.y)
  if (n && !n.isolated) { dragNode = n; n.fx = n.x; n.fy = n.y; sim?.alphaTarget(0.2).restart() }
  else { panStart = { x: p.x, y: p.y, tx: tf.x, ty: tf.y }; userMoved = true }
  dragging.value = true
}
const onMove = (ev: MouseEvent) => {
  const p = local(ev)
  if (dragNode) { const g = toGraph(p.x, p.y); dragNode.fx = g.x; dragNode.fy = g.y; downAt.moved = downAt.moved || Math.hypot(p.x - downAt.x, p.y - downAt.y) > 3; draw(); return }
  if (panStart) { tf.x = panStart.tx + (p.x - panStart.x); tf.y = panStart.ty + (p.y - panStart.y); downAt.moved = downAt.moved || Math.hypot(p.x - downAt.x, p.y - downAt.y) > 3; draw(); return }
  const n = hit(p.x, p.y)
  if (n !== hover.value) { hover.value = n; draw() }
  tip.x = p.x; tip.y = p.y
}
const releaseDrag = () => { if (dragNode) { if (!(props.scope === 'agent' && dragNode.kind === 'agent')) { dragNode.fx = null; dragNode.fy = null } sim?.alphaTarget(0) } dragNode = null; panStart = null; dragging.value = false }
const onUp = (ev: MouseEvent) => { const p = local(ev); const wasDrag = !!dragNode; releaseDrag(); if (!downAt.moved) select(hit(p.x, p.y)); else if (wasDrag) draw() }
const onLeave = () => { releaseDrag(); hover.value = null; draw() }
const onWheel = (ev: WheelEvent) => {
  const p = local(ev)
  userMoved = true
  const k2 = Math.max(0.1, Math.min(6, tf.k * Math.exp(-ev.deltaY * 0.0016)))
  tf.x = p.x - (p.x - tf.x) * (k2 / tf.k); tf.y = p.y - (p.y - tf.y) * (k2 / tf.k); tf.k = k2
  draw()
}
const onDbl = (ev: MouseEvent) => { const p = local(ev); const n = hit(p.x, p.y); if (n) open(n) }
const select = (n: GNode | null) => { selected.value = n && selected.value?.id === n.id ? null : n; draw() }
const open = (n: GNode) => {
  const raw = n.id.split(':').slice(1).join(':')
  if (n.kind === 'agent') emit('open-agent', raw)
  else if (n.kind === 'instruction') emit('open-instruction', raw)
  else if (n.kind === 'table' && props.agentId) emit('open-table', props.agentId, raw)
}
const degreeOf = (n: GNode, kind: Kind) => { let c = 0; for (const m of adjacency.get(n.id) || []) if (nodeById.get(m)?.kind === kind) c++; return c }
const neighborsOf = (n: GNode) => [...(adjacency.get(n.id) || [])].map(id => nodeById.get(id)!).filter(Boolean).filter(m => m.kind !== 'label').sort((a, b) => a.kind.localeCompare(b.kind) || a.label.localeCompare(b.label))
const kindLabel = (n: GNode) => n.kind === 'agent' ? t('knowledgeGraph.agent') : n.kind === 'instruction' ? (n.is_global ? t('knowledgeGraph.globalInstruction') : t('knowledgeGraph.instruction')) : n.kind === 'table' ? t('knowledgeGraph.table') : t('knowledgeGraph.label')

// ------------------------------------------------------------- lifecycle ---
let ro: ResizeObserver | null = null
let mo: MutationObserver | null = null
onMounted(() => {
  isDark.value = document.documentElement.classList.contains('dark')
  mo = new MutationObserver(() => { const d = document.documentElement.classList.contains('dark'); if (d !== isDark.value) { isDark.value = d; fetchGraph() } })
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
  ro = new ResizeObserver(() => { resize(); if (!userMoved) fit(false) })
  if (root.value) ro.observe(root.value)
  resize()
  fetchGraph()
})
onBeforeUnmount(() => { sim?.stop(); ro?.disconnect(); mo?.disconnect(); if (raf) cancelAnimationFrame(raf) })
watch(() => [props.scope, props.agentId, props.showAll], () => fetchGraph())
watch([showLabels, showGlobal], () => rebuild())
watch(showIsolated, () => fit(true))
watch([showTitles, query, focusCommunity], () => draw())
</script>

<style scoped>
.kg-btn { @apply h-7 w-7 rounded-md inline-flex items-center justify-center bg-white/90 dark:bg-gray-900/90 backdrop-blur-sm ring-1 ring-gray-200/80 dark:ring-gray-800 text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-200 transition-colors; }
.kg-btn-on { @apply text-blue-600 dark:text-blue-400 ring-blue-200 dark:ring-blue-500/40; }
</style>
