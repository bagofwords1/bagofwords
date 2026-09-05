<template>
  <div ref="canvasElement" class="relative overflow-hidden rounded-lg border border-gray-200 dark:border-gray-800 bg-gray-50/50 dark:bg-gray-950/30" style="height: clamp(380px, 52vh, 560px)" data-testid="tables-erd">
    <div class="absolute top-3 start-3 end-3 z-10 flex items-start justify-between gap-2 pointer-events-none">
      <div class="flex flex-wrap items-center gap-1 rounded-md border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 p-1 shadow-sm pointer-events-auto">
        <button v-if="canUpdate" class="control px-2 gap-1.5" @click="searchOpen = !searchOpen" :aria-expanded="searchOpen">
          <UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" />{{ t('tableErd.addTables') }}
        </button>
        <button v-for="action in actions" :key="action.label" class="control w-7" :title="action.label" :aria-label="action.label" @click="action.run">
          <UIcon :name="action.icon" class="w-3.5 h-3.5" />
        </button>
        <button v-if="focused" class="control px-2" @click="clearFocus">{{ t('tableErd.clearFocus') }}</button>
      </div>
      <select v-if="showStats" v-model="overlay" :aria-label="t('tableErd.overlay')" class="pointer-events-auto h-8 max-w-36 rounded-md border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 text-[11px] text-gray-600 dark:text-gray-300 px-2 shadow-sm">
        <option v-for="key in ['none', 'usage', 'lastUsed', 'feedback']" :key="key" :value="key">{{ t(`tableErd.${key}`) }}</option>
      </select>
    </div>
    <div v-if="searchOpen" class="absolute start-3 top-14 z-10 w-60 max-h-[60%] overflow-auto rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 shadow-md p-1" data-testid="erd-discovery">
      <div class="px-2 py-2 text-[10px] text-gray-500">{{ t('tableErd.searchHint') }}</div>
      <button v-for="table in matchingTables.slice(0, resultLimit)" :key="tableId(table)" class="flex items-center gap-2 w-full rounded px-2 py-2 text-start hover:bg-gray-50 dark:hover:bg-gray-800" @click="reveal(tableId(table))">
        <UIcon :name="activeIds.has(tableId(table)) ? 'i-heroicons-check' : 'i-heroicons-table-cells'" class="w-3.5 h-3.5 shrink-0 text-gray-400" />
        <span class="truncate text-[11px] font-mono" dir="ltr">{{ table.name }}</span>
      </button>
      <div v-if="!matchingTables.length" class="p-2 text-xs text-gray-500">{{ t('tableErd.noMatches') }}</div>
      <button v-if="matchingTables.length > resultLimit" class="control px-2 w-full" @click="resultLimit += 50">{{ t('tableErd.showMore') }}</button>
    </div>
    <VueFlow class="erd-flow" :id="flowId" v-model:nodes="nodes" :edges="edges" :min-zoom="0.15" :max-zoom="1.8" :nodes-connectable="false" :edges-updatable="false"
      :delete-key-code="null" :selection-key-code="null" :multi-selection-key-code="null" :select-nodes-on-drag="false" :zoom-on-double-click="false"
      @nodes-initialized="initializeLayout" @node-click="onNodeClick" @pane-click="clearFocus" @node-drag-stop="rememberPositions">
      <template #node-table="nodeProps"><TableCanvasNode v-bind="nodeProps" /></template>
      <template #edge-self="edge"><BaseEdge :path="selfPath(edge)" :marker-end="edge.markerEnd" :style="edge.style" /></template>
    </VueFlow>
    <div v-if="!nodes.length" class="absolute inset-0 flex items-center justify-center pointer-events-none">
      <div class="text-center max-w-60 px-3"><UIcon name="i-heroicons-share" class="w-6 h-6 text-gray-300 mb-2" /><p class="text-xs text-gray-500 dark:text-gray-400">{{ t(canUpdate ? 'tableErd.empty' : 'tableErd.noMatches') }}</p></div>
    </div>
    <div v-if="focusedTable" class="absolute end-3 bottom-12 z-10 w-60 max-h-[48%] overflow-auto rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 p-3 shadow-sm" data-testid="erd-details">
      <div class="flex justify-between items-center gap-2"><span class="font-mono text-xs truncate dark:text-gray-200" dir="ltr">{{ focusedTable.name }}</span><button class="control w-5" :aria-label="t('tableErd.clearFocus')" @click="clearFocus"><UIcon name="i-heroicons-x-mark" class="w-3 h-3" /></button></div>
      <div v-if="filtering && !matchIds.has(focused!)" class="mt-2 text-[10px] text-gray-500">{{ t('tableErd.outsideFilters') }}</div>
      <p v-if="!focusedLinks.length" class="text-[11px] text-gray-500 mt-2">{{ t('tableErd.noRelationships') }}</p>
      <div v-for="link in focusedLinks" :key="link.id" class="border-t border-gray-100 dark:border-gray-800 mt-2 pt-2">
        <div v-for="pair in link.columns" :key="pair.from + pair.to" class="text-[10px] text-gray-600 dark:text-gray-400 break-words font-mono" dir="ltr">{{ byId.get(link.source)?.name }}.{{ pair.from }} → {{ byId.get(link.target)?.name }}.{{ pair.to }}</div>
      </div>
      <p v-if="graph.unresolved.get(focused!)" class="mt-2 text-[10px] text-gray-500">{{ t('tableErd.unresolved') }}</p>
      <details v-if="focusedTable.columns?.length" class="mt-3 text-[10px] text-gray-500"><summary class="cursor-pointer">{{ t('tableErd.columns', { count: focusedTable.columns.length }) }}</summary>
        <div v-for="column in focusedTable.columns" :key="column.name" class="flex justify-between gap-3 py-1 font-mono" dir="ltr"><span>{{ column.name }}</span><span class="text-gray-400">{{ column.dtype || column.type }}</span></div>
      </details>
    </div>
    <div class="absolute bottom-0 inset-x-0 flex flex-wrap items-center justify-between gap-2 border-t border-gray-200/80 dark:border-gray-800 bg-white/95 dark:bg-gray-900/95 px-3 py-2 text-[10px] text-gray-500">
      <div class="flex items-center gap-3"><span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-sm border border-gray-400 bg-gray-100 dark:bg-gray-700" />{{ t('tableErd.selected') }}</span><span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-sm border border-dashed border-gray-400" />{{ t('tableErd.notSelected') }}</span></div>
      <span><span v-if="omittedSelected" class="me-2">{{ t('tableErd.omittedSelected', { count: omittedSelected }) }}</span>{{ t('tableErd.visibleCount', { shown: nodes.length, total: tables.length }) }}<button v-if="visibleIds.size > limit" class="ms-2 text-blue-600" @click="limit += 100">{{ t('tableErd.showMore') }}</button></span>
    </div>
  </div>
</template>
<script setup lang="ts">
import { VueFlow, useVueFlow, MarkerType, BaseEdge, type Node } from '@vue-flow/core'
import { Graph } from 'dagre-d3-es/src/graphlib/graph.js'
import { layout } from 'dagre-d3-es/src/dagre/layout.js'
import { tableGraph, tableId, visibleTableIds, type GraphTable } from '~/utils/tableGraph'
import TableCanvasNode from './TableCanvasNode.vue'
const props = defineProps<{ tables: GraphTable[]; activeIds: Set<string>; matchIds: Set<string>; filtering: boolean; canUpdate: boolean; showStats: boolean }>()
const emit = defineEmits<{ toggle: [id: string, value: boolean] }>()
const { t, locale } = useI18n()
const flowId = `table-erd-${useId()}`
const { fitView, zoomIn, zoomOut, getViewport, setViewport } = useVueFlow({ id: flowId })
const canvasElement = ref<HTMLElement | null>(null)
const nodes = ref<Node[]>([])
const explored = ref(new Set<string>())
watch(() => props.activeIds, (active, previous) => {
  const removed = [...previous || []].filter(id => !active.has(id))
  if (removed.length) explored.value = new Set([...explored.value, ...removed])
})
const focused = ref<string | null>(null)
const searchOpen = ref(props.filtering)
watch(() => props.matchIds, () => { searchOpen.value = props.filtering })
const resultLimit = ref(50)
const limit = ref(150)
const overlay = ref('none')
const positions = new Map<string, { x: number; y: number }>()
let overview: ReturnType<typeof getViewport> | null = null
const byId = computed(() => new Map(props.tables.map(t => [tableId(t), t])))
const graph = computed(() => tableGraph(props.tables))
const visibleIds = computed(() => visibleTableIds(props.activeIds, graph.value.neighbors, explored.value))
const matchingTables = computed(() => props.tables.filter(t => props.matchIds.has(tableId(t))))
const omittedSelected = computed(() => [...props.activeIds].filter(id => !rendered.value.has(id)).length)
const focusedTable = computed(() => focused.value ? byId.value.get(focused.value) : null)
const focusedLinks = computed(() => graph.value.links.filter(l => l.source === focused.value || l.target === focused.value))
const neighborhood = computed(() => new Set(focused.value ? [focused.value, ...graph.value.neighbors.get(focused.value) || []] : []))
const rendered = computed(() => new Set(nodes.value.map(n => n.id)))
const edges = computed(() => graph.value.links.filter(l => rendered.value.has(l.source) && rendered.value.has(l.target)).map(l => ({
  id: l.id, source: l.source, target: l.target, type: l.source === l.target ? 'self' : 'smoothstep', markerEnd: MarkerType.ArrowClosed,
  style: { stroke: focused.value && (l.source === focused.value || l.target === focused.value) ? '#3b82f6' : '#9ca3af', strokeWidth: 1.2, opacity: focused.value && !(l.source === focused.value || l.target === focused.value) ? 0.2 : 0.7 },
})))
const actions = computed(() => [
  { label: t('tableErd.fit'), icon: 'i-heroicons-arrows-pointing-out', run: () => fitView({ padding: 0.12, duration: 250 }) },
  { label: t('tableErd.zoomIn'), icon: 'i-heroicons-magnifying-glass-plus', run: () => zoomIn() },
  { label: t('tableErd.zoomOut'), icon: 'i-heroicons-magnifying-glass-minus', run: () => zoomOut() },
  { label: t('tableErd.arrange'), icon: 'i-heroicons-squares-2x2', run: rearrange },
  { label: t('tableErd.reset'), icon: 'i-heroicons-arrow-uturn-left', run: () => { explored.value = new Set(); clearFocus() } },
])
function selfPath(edge: { sourceX: number; sourceY: number; targetX: number; targetY: number }) {
  const { sourceX: x, sourceY: y, targetX: tx, targetY: ty } = edge
  return `M ${x} ${y} C ${x + 50} ${y}, ${x + 50} ${y - 110}, ${x} ${y - 110} L ${tx} ${y - 110} C ${tx - 50} ${y - 110}, ${tx - 50} ${ty}, ${tx} ${ty}`
}
function rememberPositions() { for (const n of nodes.value) positions.set(n.id, { ...n.position }) }
function metric(table: GraphTable) {
  const number = (value?: number) => value == null ? '—' : new Intl.NumberFormat(locale.value).format(value)
  if (overlay.value === 'usage') return t('tableErd.usageValue', { count: number(table.usage_count) })
  if (overlay.value === 'feedback') return t('tableErd.feedbackValue', { positive: number(table.pos_feedback_count), negative: number(table.neg_feedback_count) })
  if (overlay.value === 'lastUsed') return table.last_used_at ? new Intl.DateTimeFormat(locale.value, { dateStyle: 'medium' }).format(new Date(table.last_used_at)) : t('tableErd.unknown')
  return ''
}
function toggle(id: string, value: boolean) { explored.value = new Set([...explored.value, id]); emit('toggle', id, value) }
function expand(id: string) {
  explored.value = new Set([...explored.value, id, ...graph.value.neighbors.get(id) || []])
}
async function reveal(id: string) {
  explored.value = new Set([...explored.value, id]); searchOpen.value = false
  focus(id)
}
function focus(id: string) {
  if (!focused.value) overview = getViewport()
  focused.value = id
  nextTick(() => fitView({ nodes: [...neighborhood.value], padding: 0.4, duration: 250, maxZoom: 1 }))
}
function onNodeClick({ node }: { node: Node }) { focus(node.id) }
function clearFocus() { focused.value = null; if (overview) { setViewport(overview, { duration: 250 }); overview = null } }
let arranged = false
function initializeLayout() {
  if (!arranged && nodes.value.length) { arranged = true; rearrange() }
}
function rearrange() {
  const narrow = (canvasElement.value?.clientWidth || 900) < 760
  const g = new Graph().setGraph({ rankdir: narrow ? 'TB' : 'LR', nodesep: 28, ranksep: narrow ? 40 : 80, marginx: 12, marginy: 12 }).setDefaultEdgeLabel(() => ({}))
  for (const n of nodes.value) g.setNode(n.id, { width: 232, height: 140 })
  for (const l of graph.value.links) if (rendered.value.has(l.source) && rendered.value.has(l.target) && l.source !== l.target) g.setEdge(l.source, l.target)
  layout(g)
  nodes.value = nodes.value.map(n => { const p = g.node(n.id); const position = { x: p.x - 116, y: p.y - 70 }; positions.set(n.id, position); return { ...n, position } })
  nextTick(() => fitView({ padding: 0.12, maxZoom: 1, duration: 250 }))
}
watch([visibleIds, () => props.tables, () => props.activeIds, focused, overlay, locale], () => {
  rememberPositions()
  const previous = new Set(nodes.value.map(node => node.id))
  // Preserve the current neighborhood during bulk changes. A search/expand
  // target takes priority without lifting the rendering bound for the catalog.
  const priority = (id: string) => focused.value === id ? 0 : explored.value.has(id) ? 1 : previous.has(id) ? 2 : props.activeIds.has(id) ? 3 : 4
  const ids = [...visibleIds.value].filter(id => byId.value.has(id)).sort((a, b) => priority(a) - priority(b)).slice(0, limit.value)
  nodes.value = ids.map((id, i) => {
    const table = byId.value.get(id)!
    if (!positions.has(id)) {
      const neighbor = [...graph.value.neighbors.get(id) || []].find(n => positions.has(n))
      const base = neighbor ? positions.get(neighbor)! : { x: (i % 4) * 330, y: Math.floor(i / 4) * 200 }
      let p = { x: base.x + (neighbor ? 340 : 0), y: base.y }
      while ([...positions.values()].some(other => Math.abs(other.x - p.x) < 260 && Math.abs(other.y - p.y) < 170)) p.y += 190
      positions.set(id, p)
    }
    const keyColumns = new Set([...(table.pks || []).map(c => c.name), ...(table.fks || []).map(fk => fk.column?.name || '')])
    for (const l of graph.value.links) { if (l.source === id) l.columns.forEach(c => keyColumns.add(c.from)); if (l.target === id) l.columns.forEach(c => keyColumns.add(c.to)) }
    return { id, type: 'table', position: positions.get(id)!, style: { opacity: focused.value && !neighborhood.value.has(id) ? 0.3 : 1 }, data: {
      table, active: props.activeIds.has(id), canUpdate: props.canUpdate, focused: focused.value === id, keyColumns: [...keyColumns].filter(Boolean), metric: metric(table),
      hiddenNeighbors: [...graph.value.neighbors.get(id) || []].filter(n => !visibleIds.value.has(n)).length,
      toggle: (value: boolean) => toggle(id, value), expand: () => expand(id),
    } }
  })
}, { immediate: true })
watch(limit, () => { explored.value = new Set(explored.value) })
</script>
<style scoped>
.erd-flow { position: absolute; inset: 48px 0 36px; height: auto; }
.control { @apply inline-flex h-6 items-center justify-center rounded text-[11px] text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800; }
:deep(.vue-flow__attribution) { display: none; }
</style>
