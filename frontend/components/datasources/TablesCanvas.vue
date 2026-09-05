<template>
  <div ref="canvasElement" class="relative overflow-hidden rounded-lg border border-gray-200 dark:border-gray-800 bg-gray-50/50 dark:bg-gray-950/30" :style="{ height: `${canvasHeight}px` }" data-testid="tables-erd">
    <div class="absolute top-3 start-3 end-3 z-10 flex items-start justify-between gap-2 pointer-events-none">
      <div class="flex flex-wrap items-center gap-1 rounded-md border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 p-1 shadow-sm pointer-events-auto">
        <USelectMenu v-if="canUpdate" v-model="menuSelection" :options="menuOptions" multiple searchable value-attribute="id" option-attribute="name"
          :search-attributes="['name', 'connection_name']" :searchable-placeholder="t('tableErd.searchTables')"
          :ui-menu="{ width: 'w-80', height: 'max-h-72', option: { size: 'text-xs' } }" :popper="{ placement: 'bottom-start' }"
          @open="menuOpen = true; searchOpen = false" @close="menuOpen = false">
          <button data-testid="erd-select-tables" class="control px-2 gap-1.5"><UIcon name="i-heroicons-table-cells" class="w-3.5 h-3.5" />{{ activeIds.size ? t('tableErd.selectedCount', { count: activeIds.size }) : t('tableErd.selectTables') }}</button>
          <template #option="{ option }">
            <DataSourceIcon :type="option.connection_type" class="h-3.5 w-3.5 shrink-0" />
            <span class="min-w-0 flex-1"><span class="block truncate font-mono" dir="ltr">{{ option.name }}</span><span class="block truncate text-[10px] text-gray-400">{{ option.connection_name }}{{ option.metadata_json?.schema ? ` · ${option.metadata_json.schema}` : '' }}</span></span>
          </template>
          <template #empty>{{ t('tableErd.noMatches') }}</template>
          <template #option-empty>{{ t('tableErd.noMatches') }}</template>
        </USelectMenu>
        <button v-for="action in actions" :key="action.label" class="control w-7" :title="action.label" :aria-label="action.label" @click="action.run">
          <UIcon :name="action.icon" class="w-3.5 h-3.5" />
        </button>
        <button v-if="focused" class="control px-2" @click="clearFocus">{{ t('tableErd.clearFocus') }}</button>
      </div>
    </div>
    <div class="absolute end-3 top-3 z-10 pointer-events-auto">
      <button v-if="fullscreen" type="button" class="inline-flex items-center gap-1.5 rounded-md border border-gray-200/90 bg-white/95 px-2.5 py-1.5 text-[11px] font-medium text-gray-600 shadow-sm transition-colors hover:bg-gray-50 hover:text-gray-900 dark:border-gray-700 dark:bg-gray-900/95 dark:text-gray-300 dark:hover:bg-gray-800 dark:hover:text-white" :aria-label="t('tableErd.exitFullscreen')" :title="t('tableErd.exitFullscreen')" @click="emit('toggleFullscreen')">
        <UIcon name="i-heroicons-arrows-pointing-in" class="h-3.5 w-3.5" />
        {{ t('tableErd.exitFullscreen') }}
      </button>
      <button v-else type="button" class="control h-8 w-8 rounded-md border border-gray-200/90 bg-white/95 shadow-sm transition-colors hover:bg-gray-50 hover:text-gray-900 dark:border-gray-700 dark:bg-gray-900/95 dark:hover:bg-gray-800 dark:hover:text-white" :aria-label="t('tableErd.fullscreen')" :title="t('tableErd.fullscreen')" @click="emit('toggleFullscreen')">
        <UIcon name="i-heroicons-arrows-pointing-out" class="h-3.5 w-3.5" />
      </button>
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
    <VueFlow class="erd-flow" :style="flowStyle" :id="flowId" v-model:nodes="nodes" :edges="edges" :min-zoom="0.015" :max-zoom="1.8" :only-render-visible-elements="arranged" :nodes-connectable="false" :edges-updatable="false"
      :delete-key-code="null" :selection-key-code="null" :multi-selection-key-code="null" :select-nodes-on-drag="false" :zoom-on-double-click="false"
      @nodes-initialized="initializeLayout" @node-click="onNodeClick" @pane-click="clearFocus" @node-drag-stop="rememberPositions">
      <template #node-table="nodeProps"><TableCanvasNode v-bind="nodeProps" /></template>
      <template #edge-self="edge"><BaseEdge :path="selfPath(edge)" :marker-end="edge.markerEnd" :style="edge.style" /></template>
    </VueFlow>
    <div v-if="!nodes.length" class="absolute inset-0 flex items-center justify-center pointer-events-none">
      <div class="text-center max-w-60 px-3"><UIcon name="i-heroicons-share" class="w-6 h-6 text-gray-300 mb-2" /><p class="text-xs text-gray-500 dark:text-gray-400">{{ t(canUpdate ? 'tableErd.empty' : 'tableErd.noMatches') }}</p></div>
    </div>
    <div v-if="focusedTable" ref="detailsPanel" :style="panelStyle" class="absolute end-3 bottom-12 z-10 w-64 overflow-auto rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 p-3 shadow-sm" data-testid="erd-details">
      <div class="flex justify-between items-center gap-2"><span class="font-mono text-xs truncate dark:text-gray-200" dir="ltr">{{ focusedTable.name }}</span><button class="control w-5" :aria-label="t('tableErd.clearFocus')" @click="clearFocus"><UIcon name="i-heroicons-x-mark" class="w-3 h-3" /></button></div>
      <div v-if="filtering && !matchIds.has(focused!)" class="mt-2 text-[10px] text-gray-500">{{ t('tableErd.outsideFilters') }}</div>
      <p v-if="!focusedLinks.length" class="text-[11px] text-gray-500 mt-2">{{ t('tableErd.noRelationships') }}</p>
      <div v-for="link in columnsOpen ? [] : focusedLinks" :key="link.id" class="border-t border-gray-100 dark:border-gray-800 mt-2 pt-2">
        <p v-if="link.suggested" class="text-[10px] text-gray-500 mb-1">{{ t('tableErd.suggestedHint') }}</p>
        <p v-if="byId.get(link.source)?.connection_id !== byId.get(link.target)?.connection_id" class="text-[10px] text-gray-400 mb-1">{{ byId.get(link.source)?.connection_name }} → {{ byId.get(link.target)?.connection_name }}</p>
        <div v-for="pair in link.columns" :key="pair.from + pair.to" class="text-[10px] text-gray-600 dark:text-gray-400 break-words font-mono" dir="ltr">{{ byId.get(link.source)?.name }}.{{ pair.from }} → {{ byId.get(link.target)?.name }}.{{ pair.to }}</div>
      </div>
      <p v-if="graph.unresolved.get(focused!)" class="mt-2 text-[10px] text-gray-500">{{ t('tableErd.unresolved') }}</p>
      <details :open="columnsOpen" class="mt-3 text-[10px] text-gray-500" @toggle="columnsOpen = ($event.target as HTMLDetailsElement).open">
        <summary class="cursor-pointer">{{ t('tableErd.columns', { count: focusedTable.columns?.length || 0 }) }}</summary>
        <input ref="columnSearchInput" v-model="columnSearch" :placeholder="t('tableErd.searchColumns')" :aria-label="t('tableErd.searchColumns')" class="my-2 w-full rounded border border-gray-200 dark:border-gray-700 bg-transparent px-2 py-1.5 text-xs" />
        <div class="max-h-64 overflow-auto overscroll-contain">
          <div v-for="column in filteredColumns.slice(0, columnLimit)" :key="column.name" data-testid="erd-column" class="flex justify-between gap-3 py-1.5 font-mono" dir="ltr"><span class="break-all">{{ column.name }}</span><span class="text-gray-400 shrink-0">{{ column.dtype || column.type }}</span></div>
          <p v-if="!filteredColumns.length" class="py-2">{{ t('tableErd.noColumns') }}</p>
          <button v-if="filteredColumns.length > columnLimit" class="control px-2 w-full" @click="columnLimit += 50">{{ t('tableErd.showMore') }}</button>
        </div>
      </details>
      <TableRecentPrompts v-if="canViewPrompts && agentId && focusedTable.id" :agent-id="agentId" :table-id="focusedTable.id" />
    </div>
    <div class="absolute bottom-0 inset-x-0 flex flex-wrap items-center justify-between gap-2 border-t border-gray-200/80 dark:border-gray-800 bg-white/95 dark:bg-gray-900/95 px-3 py-2 text-[10px] text-gray-500">
      <div class="flex items-center gap-3"><span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-sm border border-blue-500" />{{ t('tableErd.selected') }}</span><span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-sm border border-dotted border-gray-400" />{{ t('tableErd.notSelected') }}</span><span v-if="hasSuggestions" class="flex items-center gap-1.5"><span class="w-4 border-t border-dashed border-gray-400" />{{ t('tableErd.suggested') }}</span></div>
      <span data-testid="erd-visible-count">{{ t('tableErd.visibleCount', { shown: nodes.length, total: tables.length }) }}</span>
    </div>
  </div>
</template>
<script setup lang="ts">
import { useElementSize, useEventListener } from '@vueuse/core'
import { VueFlow, useVueFlow, MarkerType, BaseEdge, type Node } from '@vue-flow/core'
import { Graph } from 'dagre-d3-es/src/graphlib/graph.js'
import { layout } from 'dagre-d3-es/src/dagre/layout.js'
import { TABLE_NODE_WIDTH, TABLE_NODE_HEIGHT, tableGraph, tableId, visibleTableIds, type GraphTable } from '~/utils/tableGraph'
import TableCanvasNode from './TableCanvasNode.vue'
import TableRecentPrompts from './TableRecentPrompts.vue'
import DataSourceIcon from '@/components/DataSourceIcon.vue'
const props = defineProps<{ agentId?: string; canViewPrompts?: boolean; tables: GraphTable[]; activeIds: Set<string>; matchIds: Set<string>; filtering: boolean; canUpdate: boolean; showStats: boolean; editableQueryIds?: Set<string>; fullscreen?: boolean; bottomInset?: number }>()
const emit = defineEmits<{ toggle: [id: string, value: boolean]; editQuery: [id: string]; toggleFullscreen: []; ready: [] }>()
const { t, locale } = useI18n()
const flowId = `table-erd-${useId()}`
const { zoomIn, zoomOut, getViewport, setViewport } = useVueFlow({ id: flowId })
const canvasElement = ref<HTMLElement | null>(null)
const canvasHeight = ref(460)
const { width: canvasWidth } = useElementSize(canvasElement)
function resizeCanvas() {
  if (!canvasElement.value) return
  canvasHeight.value = Math.max(320, window.innerHeight - Math.max(0, canvasElement.value.getBoundingClientRect().top) - (props.bottomInset || 72))
}
onMounted(() => nextTick(resizeCanvas))
useEventListener('resize', resizeCanvas)
watch([canvasWidth, () => props.filtering, locale], () => { if (canvasWidth.value > 0) nextTick(resizeCanvas) })
let embeddedViewport: ReturnType<typeof getViewport> | null = null
watch(() => props.fullscreen, (full) => {
  const previousWidth = canvasWidth.value
  const previousHeight = canvasHeight.value
  if (full) embeddedViewport = getViewport()
  nextTick(() => {
    resizeCanvas()
    nextTick(() => {
      if (!full && embeddedViewport) { setViewport(embeddedViewport); embeddedViewport = null }
      else if (full) {
        const viewport = getViewport()
        setViewport({ ...viewport, x: viewport.x + (canvasWidth.value - previousWidth) / 2, y: viewport.y + (canvasHeight.value - previousHeight) / 2 })
      }
    })
  })
})
// Vue Flow keeps an LTR coordinate surface even when its surrounding UI is RTL.
const flowStyle = computed(() => focusedTable.value ? canvasWidth.value >= 700 ? { [(['he', 'ar'].includes(locale.value) ? 'insetInlineStart' : 'insetInlineEnd')]: '280px' } : { bottom: '250px' } : {})
const panelStyle = computed(() => canvasWidth.value >= 700 ? { top: '52px' } : { height: '200px', width: 'calc(100% - 24px)' })
const detailsPanel = ref<HTMLElement | null>(null)
const columnSearchInput = ref<HTMLInputElement | null>(null)
const columnsOpen = ref(false)
const columnSearch = ref('')
const columnLimit = ref(50)
const filteredColumns = computed(() => (focusedTable.value?.columns || []).filter(column => column.name.toLowerCase().includes(columnSearch.value.toLowerCase())))
watch(columnSearch, () => { columnLimit.value = 50 })
const nodes = ref<Node[]>([])
const explored = ref(new Set<string>())
watch(() => props.activeIds, (active, previous) => {
  const removed = [...previous || []].filter(id => !active.has(id))
  if (removed.length) explored.value = new Set([...explored.value, ...removed])
})
const focused = ref<string | null>(null)
const menuOpen = ref(false)
const searchOpen = ref(props.filtering)
watch(() => props.matchIds, () => { if (!menuOpen.value) searchOpen.value = props.filtering })
const resultLimit = ref(50)
const positions = new Map<string, { x: number; y: number }>()
let overview: ReturnType<typeof getViewport> | null = null
const byId = computed(() => new Map(props.tables.map(t => [tableId(t), t])))
const graph = computed(() => tableGraph(props.tables))
const visibleIds = computed(() => visibleTableIds(props.activeIds, graph.value.neighbors, explored.value))
const matchingTables = computed(() => props.tables.filter(t => props.matchIds.has(tableId(t))))
const menuOptions = computed(() => matchingTables.value.map(table => ({ ...table, id: tableId(table) })).sort((a, b) => Number(props.activeIds.has(b.id)) - Number(props.activeIds.has(a.id)) || a.name.localeCompare(b.name) || a.id.localeCompare(b.id)))
const menuSelection = computed({
  get: () => [...props.activeIds],
  set: (ids: string[]) => {
    const next = new Set(ids)
    for (const id of new Set([...props.activeIds, ...next])) if (props.activeIds.has(id) !== next.has(id)) toggle(id, next.has(id))
    const added = ids.find(id => !props.activeIds.has(id))
    if (added) focus(added)
  },
})
const focusedTable = computed(() => focused.value ? byId.value.get(focused.value) : null)
const focusedLinks = computed(() => graph.value.links.filter(l => l.source === focused.value || l.target === focused.value))
const neighborhood = computed(() => new Set(focused.value ? [focused.value, ...graph.value.neighbors.get(focused.value) || []] : []))
const rendered = computed(() => new Set(nodes.value.map(n => n.id)))
const hasSuggestions = computed(() => graph.value.links.some(l => l.suggested && rendered.value.has(l.source) && rendered.value.has(l.target)))
const edges = computed(() => graph.value.links.filter(l => rendered.value.has(l.source) && rendered.value.has(l.target)).map(l => ({
  id: l.id, source: l.source, target: l.target, type: l.source === l.target ? 'self' : 'smoothstep', markerEnd: MarkerType.ArrowClosed,
  class: l.suggested ? 'erd-suggested-edge' : '',
  style: { strokeDasharray: l.suggested ? '5 4' : undefined, stroke: focused.value && (l.source === focused.value || l.target === focused.value) ? '#3b82f6' : '#9ca3af', strokeWidth: 1.2, opacity: focused.value && !(l.source === focused.value || l.target === focused.value) ? 0.2 : 0.7 },
})))
const actions = computed(() => [
  { label: t('tableErd.fit'), icon: 'i-heroicons-viewfinder-circle', run: () => fitNodes() },
  { label: t('tableErd.zoomIn'), icon: 'i-heroicons-magnifying-glass-plus', run: () => zoomIn() },
  { label: t('tableErd.zoomOut'), icon: 'i-heroicons-magnifying-glass-minus', run: () => zoomOut() },
  { label: t('tableErd.arrange'), icon: 'i-heroicons-squares-2x2', run: () => rearrange() },
  { label: t('tableErd.reset'), icon: 'i-heroicons-arrow-uturn-left', run: () => { explored.value = new Set(); clearFocus() } },
])
function selfPath(edge: { sourceX: number; sourceY: number; targetX: number; targetY: number }) {
  const { sourceX: x, sourceY: y, targetX: tx, targetY: ty } = edge
  return `M ${x} ${y} C ${x + 50} ${y}, ${x + 50} ${y - 110}, ${x} ${y - 110} L ${tx} ${y - 110} C ${tx - 50} ${y - 110}, ${tx - 50} ${ty}, ${tx} ${ty}`
}
function rememberPositions() { for (const n of nodes.value) positions.set(n.id, { ...n.position }) }
function fitNodes(ids?: Set<string>, padding = 0.08, duration = 250, minimumZoom = 0.015) {
  const rows = nodes.value.filter(node => !ids || ids.has(node.id))
  if (!rows.length || !canvasElement.value) return
  // Fixed card geometry lets us fit offscreen cards before Vue Flow mounts
  // them, without waiting for DOM measurements or stale computed positions.
  const left = Math.min(...rows.map(n => n.position.x)), top = Math.min(...rows.map(n => n.position.y))
  const width = Math.max(...rows.map(n => n.position.x + TABLE_NODE_WIDTH)) - left
  const height = Math.max(...rows.map(n => n.position.y + TABLE_NODE_HEIGHT)) - top
  const viewportWidth = canvasElement.value.clientWidth - (focusedTable.value && canvasWidth.value >= 700 ? 280 : 0), viewportHeight = canvasElement.value.clientHeight - 84 - (focusedTable.value && canvasWidth.value < 700 ? 214 : 0)
  const zoom = Math.max(minimumZoom, Math.min(1, viewportWidth / (width * (1 + 2 * padding)), viewportHeight / (height * (1 + 2 * padding))))
  return setViewport({ x: (viewportWidth - width * zoom) / 2 - left * zoom, y: (viewportHeight - height * zoom) / 2 - top * zoom, zoom }, { duration })
}
function toggle(id: string, value: boolean) { explored.value = new Set([...explored.value, id]); emit('toggle', id, value) }
function expand(id: string) {
  explored.value = new Set([...explored.value, id, ...graph.value.neighbors.get(id) || []])
  focus(id)
}
async function reveal(id: string) {
  explored.value = new Set([...explored.value, id]); searchOpen.value = false
  focus(id)
}
function focus(id: string) {
  if (!focused.value) overview = getViewport()
  if (focused.value !== id) { columnsOpen.value = false; columnSearch.value = ''; columnLimit.value = 50 }
  focused.value = id
  nextTick(() => fitNodes(canvasWidth.value < 700 ? new Set([id]) : neighborhood.value, 0.06))
}
async function openColumns(id: string) {
  focus(id)
  columnsOpen.value = true
  columnSearch.value = ''
  columnLimit.value = 50
  await nextTick()
  if (focused.value !== id) return
  // Make the destination visible even when the previous table's details were scrolled.
  if (detailsPanel.value) detailsPanel.value.scrollTop = 0
  columnSearchInput.value?.focus({ preventScroll: true })
}
function onNodeClick({ node }: { node: Node }) { focus(node.id) }
function clearFocus() { focused.value = null; if (overview) { setViewport(overview, { duration: 250 }); overview = null } }
const arranged = ref(false)
async function initializeLayout() {
  if (!arranged.value && nodes.value.length) {
    arranged.value = true
    rearrange(true)
    // Wait for positions and the initial viewport to reach the DOM before revealing it.
    await nextTick()
    await nextTick()
    emit('ready')
  }
}
onMounted(() => { if (!nodes.value.length) emit('ready') })
function rearrange(initial = false) {
  const narrow = (canvasElement.value?.clientWidth || 900) < 760
  // Lay out connected neighborhoods independently, then pack them into rows.
  // Hundreds of unrelated tables must not become a single enormous Dagre rank.
  const remaining = new Set(nodes.value.map(n => n.id))
  const groups: { width: number; height: number; points: Map<string, { x: number; y: number }> }[] = []
  while (remaining.size) {
    const ids = [remaining.values().next().value!]
    remaining.delete(ids[0])
    for (let i = 0; i < ids.length; i++) for (const id of graph.value.neighbors.get(ids[i]) || []) if (remaining.delete(id)) ids.push(id)
    const g = new Graph().setGraph({ rankdir: narrow ? 'TB' : 'LR', nodesep: 28, ranksep: narrow ? 40 : 80 }).setDefaultEdgeLabel(() => ({}))
    for (const id of ids) g.setNode(id, { width: TABLE_NODE_WIDTH, height: TABLE_NODE_HEIGHT })
    for (const l of graph.value.links) if (g.hasNode(l.source) && g.hasNode(l.target) && l.source !== l.target) g.setEdge(l.source, l.target)
    if (ids.length > 1) layout(g)
    groups.push(ids.length === 1 ? { width: TABLE_NODE_WIDTH, height: TABLE_NODE_HEIGHT, points: new Map([[ids[0], {x: 0, y: 0}]]) } : {
      width: g.graph().width, height: g.graph().height,
      points: new Map(ids.map(id => { const p = g.node(id); return [id, {x: p.x - TABLE_NODE_WIDTH / 2, y: p.y - TABLE_NODE_HEIGHT / 2}] })),
    })
  }
  const width = Math.max(...groups.map(g => g.width), Math.sqrt(groups.reduce((area, g) => area + (g.width + 40) * (g.height + 40), 0) * (narrow ? 1 : 1.8)))
  let x = 0, y = 0, rowHeight = 0
  for (const group of groups) {
    if (x && x + group.width > width) { x = 0; y += rowHeight + 40; rowHeight = 0 }
    for (const [id, p] of group.points) positions.set(id, {x: p.x + x, y: p.y + y})
    x += group.width + 40; rowHeight = Math.max(rowHeight, group.height)
  }
  nodes.value = nodes.value.map(n => ({ ...n, position: positions.get(n.id)!, computedPosition: { ...positions.get(n.id)!, z: 0 } }))
  nextTick(() => fitNodes(undefined, 0.08, 0, initial ? 0.55 : 0.015))
}
watch([visibleIds, () => props.tables, () => props.activeIds, focused, locale, () => props.showStats, () => props.canUpdate, () => props.editableQueryIds], () => {
  rememberPositions()
  const previous = new Set(nodes.value.map(node => node.id))
  const ids = [...visibleIds.value].filter(id => byId.value.has(id))
  nodes.value = ids.map((id, i) => {
    const table = byId.value.get(id)!
    if (!positions.has(id)) {
      const neighbor = [...graph.value.neighbors.get(id) || []].find(n => positions.has(n))
      const base = neighbor ? positions.get(neighbor)! : { x: (i % 4) * 330, y: Math.floor(i / 4) * 200 }
      let p = { x: base.x + (neighbor ? 340 : 0), y: base.y }
      while ([...positions.values()].some(other => Math.abs(other.x - p.x) < 310 && Math.abs(other.y - p.y) < 200)) p.y += 220
      positions.set(id, p)
    }
    const keyColumns = new Set([...(table.pks || []).map(c => c.name), ...(table.fks || []).map(fk => fk.column?.name || '')])
    for (const l of graph.value.links) { if (l.source === id) l.columns.forEach(c => keyColumns.add(c.from)); if (l.target === id) l.columns.forEach(c => keyColumns.add(c.to)) }
    // Offscreen nodes have no mounted wrapper to recompute their coordinates.
    // Keep Vue Flow's viewport index aligned when we move them programmatically.
    return { id, type: 'table', position: positions.get(id)!, computedPosition: { ...positions.get(id)!, z: 0 }, dimensions: { width: TABLE_NODE_WIDTH, height: TABLE_NODE_HEIGHT }, style: { opacity: focused.value && !neighborhood.value.has(id) ? 0.3 : 1 }, data: {
      table, active: props.activeIds.has(id), canUpdate: props.canUpdate, focused: focused.value === id, keyColumns: [...keyColumns].filter(Boolean), showStats: props.showStats,
      editQuery: table.custom_query_id && props.editableQueryIds?.has(table.custom_query_id) ? () => emit('editQuery', table.custom_query_id!) : undefined,
      hiddenNeighbors: [...graph.value.neighbors.get(id) || []].filter(n => !visibleIds.value.has(n)).length,
      openColumns: () => openColumns(id),
      toggle: (value: boolean) => toggle(id, value), expand: () => expand(id),
    } }
  })
  if (ids.filter(id => !previous.has(id)).length > 25) nextTick(() => rearrange())
}, { immediate: true })
</script>
<style scoped>
.erd-flow { position: absolute; inset: 48px 0 36px; width: auto; height: auto; }
.control { @apply inline-flex h-6 items-center justify-center rounded text-[11px] text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800; }
:deep(.vue-flow__attribution) { display: none; }
</style>
