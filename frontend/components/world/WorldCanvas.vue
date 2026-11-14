<template>
  <div ref="host" class="world-canvas relative h-full w-full overflow-hidden" @pointerleave="hover = null">
    <canvas ref="canvas" class="h-full w-full touch-none outline-none" tabindex="0" role="img"
      :aria-label="t('world.mapLabel')" @pointerdown="down" @pointermove="move" @pointerup="up"
      @pointercancel="drag = null" @wheel.prevent="wheel" @keydown="keyboard" />
    <div v-if="hover && !drag" class="pointer-events-none absolute rounded-lg border border-line bg-surface-elevated/95 px-3 py-2 shadow-lg backdrop-blur"
      :style="{ left: `${Math.min(hover.x + 16, width - 220)}px`, top: `${Math.max(8, hover.y - 52)}px`, maxWidth: '210px' }">
      <p class="text-2xs text-ink-muted">{{ t(`world.types.${hover.point.node.type}`) }}</p>
      <p class="truncate text-xs font-medium text-ink">{{ hover.point.node.label }}</p>
    </div>
    <div class="absolute bottom-5 start-5 flex items-center gap-1 rounded-lg border border-line bg-surface-elevated/90 p-1 shadow-sm backdrop-blur">
      <UButton color="gray" variant="ghost" icon="i-heroicons-minus" :aria-label="t('world.zoomOut')" @click="zoomBy(.8)" />
      <span class="nums w-12 text-center text-2xs text-ink-muted">{{ Math.round(camera.scale * 100) }}%</span>
      <UButton color="gray" variant="ghost" icon="i-heroicons-plus" :aria-label="t('world.zoomIn')" @click="zoomBy(1.25)" />
      <span class="mx-1 h-4 border-s border-line" />
      <UButton color="gray" variant="ghost" icon="i-heroicons-arrows-pointing-out" :aria-label="t('world.fit')" @click="fit" />
      <UButton color="gray" variant="ghost" :icon="paused ? 'i-heroicons-play' : 'i-heroicons-pause'" :aria-label="paused ? t('world.resume') : t('world.pause')" @click="paused = !paused" />
    </div>
  </div>
</template>

<script setup lang="ts">
import type { WorldNode, WorldEdge } from '~/composables/useWorldGraph'
import { layoutWorld, worldColors, type WorldPoint } from '~/utils/worldGraph'
const props = defineProps<{ nodes: WorldNode[]; edges: WorldEdge[]; visibleIds: Set<string>; selected: string | null; showLinks: boolean }>()
const emit = defineEmits<{ select: [id: string | null] }>()
const { t } = useI18n()
const colorMode = useColorMode()
const host = ref<HTMLDivElement>(); const canvas = ref<HTMLCanvasElement>()
const width = ref(800); const height = ref(700)
const camera = reactive({ x: 0, y: 0, scale: 1 })
const paused = ref(false)
const hover = ref<{ point: WorldPoint; x: number; y: number } | null>(null)
const drag = ref<{ x: number; y: number; cx: number; cy: number; moved: boolean } | null>(null)
let points: WorldPoint[] = []; let byId = new Map<string, WorldPoint>(); let connected = new Set<string>()
let observer: ResizeObserver; let frame = 0; let clock = 0; let lastTime = 0; let reduced = false
let dirty = true
let destination: { x: number; y: number; scale: number } | null = null
let motionQuery: MediaQueryList
let fitted = false
watch([() => props.nodes, () => props.edges], () => {
  points = layoutWorld(props.nodes, props.edges); byId = new Map(points.map(p => [p.node.id, p]));
  if (!fitted && points.length) { fit(); fitted = true }
}, { immediate: true })
watch([() => props.selected, () => props.edges], () => {
  connected = new Set(props.selected ? [props.selected] : [])
  for (const e of props.edges) {
    if (e.source === props.selected) connected.add(e.target)
    if (e.target === props.selected) connected.add(e.source)
  }
}, { immediate: true })
watch([camera, () => props.visibleIds, () => props.selected, () => props.showLinks, () => props.nodes, hover, paused], () => { dirty = true })
watch(() => colorMode.value, () => { dirty = true })
watch(() => props.selected, id => {
  const point = id ? byId.get(id) : null
  if (!point) return
  const scale = Math.max(camera.scale, .9)
  destination = { x: -point.x * scale, y: -point.y * scale + 35, scale }
})
function fit() {
  const bounds = points.filter(p => props.visibleIds.has(p.node.id))
  const { minX, maxX, minY, maxY } = bounds.reduce((b, p) => ({ minX: Math.min(b.minX, p.x), maxX: Math.max(b.maxX, p.x), minY: Math.min(b.minY, p.y), maxY: Math.max(b.maxY, p.y) }), { minX: -250, maxX: 250, minY: -250, maxY: 250 })
  destination = null
  camera.scale = Math.max(.12, Math.min(1.5, (width.value - 100) / (maxX - minX), (height.value - 230) / (maxY - minY)))
  camera.x = -(minX + maxX) / 2 * camera.scale
  camera.y = -(minY + maxY) / 2 * camera.scale + 65
  dirty = true
}
function zoomBy(factor: number) { destination = null; camera.scale = Math.max(.12, Math.min(5, camera.scale * factor)) }
function position(p: WorldPoint) {
  const drift = (p.node.type === 'agent' || p.node.type === 'folder') || reduced ? 0 : 1.8
  return { x: width.value / 2 + camera.x + (p.x + Math.sin(clock * .00035 + p.phase) * drift) * camera.scale,
    y: height.value / 2 + camera.y + (p.y + Math.cos(clock * .0003 + p.phase) * drift) * camera.scale }
}
function hit(x: number, y: number) {
  let result: WorldPoint | undefined; let distance = 18
  for (const p of points) {
    if (!props.visibleIds.has(p.node.id)) continue
    const s = position(p), d = Math.hypot(s.x - x, s.y - y)
    if (d < Math.max(9, p.radius * camera.scale + 4) && d < distance) { result = p; distance = d }
  }
  return result
}
function local(e: PointerEvent | WheelEvent) { const r = canvas.value!.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top } }
function down(e: PointerEvent) {
  if (e.button !== 0) return
  destination = null
  const p = local(e); drag.value = { ...p, cx: camera.x, cy: camera.y, moved: false }
  canvas.value?.setPointerCapture(e.pointerId)
}
function move(e: PointerEvent) {
  const p = local(e)
  if (drag.value) {
    const dx = p.x - drag.value.x, dy = p.y - drag.value.y
    if (Math.hypot(dx, dy) > 4) drag.value.moved = true
    camera.x = drag.value.cx + dx; camera.y = drag.value.cy + dy
  } else {
    const point = hit(p.x, p.y); hover.value = point ? { point, ...p } : null
    if (canvas.value) canvas.value.style.cursor = point ? 'pointer' : 'grab'
  }
}
function up(e: PointerEvent) {
  if (drag.value && !drag.value.moved) { const p = local(e); emit('select', hit(p.x, p.y)?.node.id || null) }
  drag.value = null; canvas.value?.releasePointerCapture(e.pointerId)
}
function wheel(e: WheelEvent) {
  const p = local(e), old = camera.scale
  zoomBy(Math.exp(-e.deltaY * .001))
  const ratio = camera.scale / old
  camera.x = p.x - width.value / 2 - (p.x - width.value / 2 - camera.x) * ratio
  camera.y = p.y - height.value / 2 - (p.y - height.value / 2 - camera.y) * ratio
}
function keyboard(e: KeyboardEvent) {
  destination = null
  if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', '+', '=', '-', '0', 'Escape'].includes(e.key)) e.preventDefault()
  if (e.key === '+' || e.key === '=') zoomBy(1.2)
  if (e.key === '-') zoomBy(.8)
  if (e.key === '0') fit()
  if (e.key === 'Escape') emit('select', null)
  if (e.key === 'ArrowLeft') camera.x += 35
  if (e.key === 'ArrowRight') camera.x -= 35
  if (e.key === 'ArrowUp') camera.y += 35
  if (e.key === 'ArrowDown') camera.y -= 35
}
function draw(time: number) {
  frame = requestAnimationFrame(draw)
  if (!canvas.value || document.hidden || time - lastTime < 30) return
  const delta = Math.min(time - lastTime, 50); lastTime = time
  if (destination) {
    const fraction = reduced || paused.value ? 1 : .14
    camera.x += (destination.x - camera.x) * fraction
    camera.y += (destination.y - camera.y) * fraction
    camera.scale += (destination.scale - camera.scale) * fraction
    if (Math.abs(camera.x - destination.x) + Math.abs(camera.y - destination.y) < .2) destination = null
    dirty = true
  }
  if ((paused.value || reduced || points.length > 200) && !dirty) return
  dirty = false
  if (!paused.value && !reduced) clock += delta
  const ctx = canvas.value.getContext('2d'); if (!ctx) return
  const dpr = Math.min(window.devicePixelRatio || 1, 2)
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, width.value, height.value)
  const dark = document.documentElement.classList.contains('dark')
  const ink = dark ? '#e5e9ef' : '#344039'
  const visible = points.filter(p => props.visibleIds.has(p.node.id))
  const dense = visible.length > 2000
  const anchors = visible.filter(p => (p.node.type === 'agent' || p.node.type === 'folder'))
  const labeledAnchors = new Set(anchors.filter((_, i) => camera.scale > .9 || i % Math.max(1, Math.ceil(anchors.length / 7)) === 0).map(p => p.node.id))
  const positions = new Map(visible.map(p => [p.node.id, position(p)]))
  // One stroke per layer keeps dense maps responsive without inventing or
  // sampling links. Selection shows the selected item's actual neighborhood.
  if (props.showLinks) for (const focus of [false, true]) {
    if (focus && !props.selected) continue
    ctx.beginPath()
    for (const e of props.edges) {
      const isFocus = !!props.selected && (e.source === props.selected || e.target === props.selected)
      if (isFocus !== focus || (dense && props.selected && !focus)) continue
      const a = positions.get(e.source), b = positions.get(e.target)
      if (!a || !b) continue
      ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y)
    }
    ctx.strokeStyle = focus ? (dark ? '#96cbb1aa' : '#709e8d99') : dense ? (dark ? '#a8b8c32b' : '#72878f38') : (dark ? '#a8b8c37a' : '#71858e87')
    ctx.lineWidth = focus ? 1.8 : 1; ctx.stroke()
  }
  const labels: { p: WorldPoint; s: { x: number; y: number }; r: number }[] = []
  for (const p of visible) {
    const s = positions.get(p.node.id)!, active = p.node.id === props.selected
    if (s.x < -100 || s.x > width.value + 100 || s.y < -50 || s.y > height.value + 50) continue
    const r = Math.max(dense ? .7 : 2, p.radius * camera.scale)
    ctx.globalAlpha = props.selected && !connected.has(p.node.id) ? .2 : 1
    if (active || hover.value?.point.node.id === p.node.id) {
      ctx.beginPath(); ctx.arc(s.x, s.y, r + 6, 0, Math.PI * 2)
      ctx.strokeStyle = worldColors[p.node.type] + '85'; ctx.lineWidth = 1.5; ctx.stroke()
    }
    ctx.beginPath(); ctx.arc(s.x, s.y, r, 0, Math.PI * 2)
    ctx.fillStyle = worldColors[p.node.type]; ctx.fill()
    if ((p.node.type === 'agent' || p.node.type === 'folder')) {
      ctx.beginPath(); ctx.arc(s.x, s.y, Math.max(2, r * .35), 0, Math.PI * 2)
      ctx.fillStyle = dark ? '#142922' : '#f4fffa'; ctx.fill()
    }
    if (visible.length <= 60 || labeledAnchors.has(p.node.id) || active || (camera.scale > 1.8 && connected.has(p.node.id))) { labels.push({ p, s, r }) }
    ctx.globalAlpha = 1
  }
  const occupied: { x: number; y: number; width: number }[] = []
  const priority = (point: WorldPoint) => point.node.id === props.selected ? 3 : connected.has(point.node.id) ? 2 : labeledAnchors.has(point.node.id) ? 1 : 0
  labels.sort((a, b) => priority(b.p) - priority(a.p))
  for (const { p, s, r } of labels) {
      const label = p.node.label.length > 28 ? p.node.label.slice(0, 27) + '…' : p.node.label
      ctx.font = `${(p.node.type === 'agent' || p.node.type === 'folder') ? '600' : '400'} 12px Inter, system-ui, sans-serif`
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle'
      const w = ctx.measureText(label).width + 20, y = s.y + r + 20
      if (occupied.some(box => Math.abs(box.y - y) < 28 && Math.abs(box.x - s.x) < (box.width + w) / 2 + 3)) continue
      occupied.push({ x: s.x, y, width: w })
      ctx.fillStyle = dark ? '#1c252aee' : '#ffffffee'
      ctx.beginPath(); ctx.roundRect(s.x - w / 2, y - 13, w, 26, 6); ctx.fill()
      ctx.strokeStyle = dark ? '#43515a66' : '#dde5dfcc'; ctx.lineWidth = .8; ctx.stroke()
      ctx.fillStyle = ink; ctx.fillText(label, s.x, y)
  }
}
function motionChanged() { reduced = motionQuery.matches; if (reduced) paused.value = true }
onMounted(() => {
  motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)'); motionChanged(); motionQuery.addEventListener('change', motionChanged)
  observer = new ResizeObserver(([entry]) => {
    width.value = entry.contentRect.width; height.value = entry.contentRect.height
    if (canvas.value) { const dpr = Math.min(window.devicePixelRatio || 1, 2); canvas.value.width = width.value * dpr; canvas.value.height = height.value * dpr }
    if (!drag.value) fit()
  })
  if (host.value) observer.observe(host.value)
  frame = requestAnimationFrame(draw)
})
onBeforeUnmount(() => { cancelAnimationFrame(frame); observer?.disconnect(); motionQuery?.removeEventListener('change', motionChanged) })
defineExpose({ fit })
</script>

<style scoped>
.world-canvas { background-image: radial-gradient(rgb(var(--bow-line-rgb) / .65) .65px, transparent .65px); background-size: 24px 24px; }
</style>
