<template>
  <div
    ref="container"
    class="grid-stack grid-stack-modal"
    :style="{ transform: `scale(${zoom})`, transformOrigin: 'top left' }"
  >
    <div
      v-for="widget in widgets"
      :key="`modal-${widget.id}`"
      class="grid-stack-item"
      :gs-id="`modal-${widget.id}`"
      :gs-x="widget.x"
      :gs-y="widget.y"
      :gs-w="widget.width"
      :gs-h="widget.height"
    >
      <div :class="['grid-stack-item-content','rounded','overflow-hidden','flex','flex-col','relative','p-0','shadow-sm']" :style="props.itemStyle">
        <WidgetFrame
          :widget="widget"
          :edit="false"
          :isText="widget.type === 'text'"
          :itemStyle="props.itemStyle"
          :cardBorder="props.tokens?.cardBorder || '#e5e7eb'"
        >
          <template v-if="widget.type === 'text'">
            <div class="p-2 flex-grow overflow-auto">
              <TextWidgetView
                :widget="widget"
                :themeName="props.themeName"
                :reportOverrides="props.reportOverrides"
              />
            </div>
          </template>
          <template v-else>
            <div class="flex-grow overflow-auto p-2 min-h-0">
              <RegularWidgetView
                :widget="widget"
                :themeName="props.themeName"
                :reportOverrides="props.reportOverrides"
              />
            </div>
          </template>
        </WidgetFrame>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import 'gridstack/dist/gridstack.min.css'
import { GridStack } from 'gridstack'
import { ref, onMounted, nextTick, watch, onBeforeUnmount } from 'vue'
import WidgetFrame from '@/components/dashboard/WidgetFrame.vue'
import TextWidgetView from '@/components/dashboard/text/TextWidgetView.vue'
import RegularWidgetView from '@/components/dashboard/regular/RegularWidgetView.vue'

const props = defineProps<{
  widgets: any[]
  report: any
  themeName: string | null
  reportOverrides?: any
  tokens?: any
  itemStyle?: any
  zoom?: number
}>()

const container = ref<HTMLElement | null>(null)
const grid = ref<GridStack | null>(null)

function initGrid() {
  if (!container.value) return
  grid.value = GridStack.init(
    {
      column: 12,
      cellHeight: 40,
      margin: 10,
      float: true,
      staticGrid: true,
    },
    container.value
  )
}

async function loadWidgetsIntoGrid() {
  if (!grid.value) return
  await nextTick()
  grid.value.batchUpdate()

  const current = new Map(grid.value.engine.nodes.map((n) => [n.id, n]))
  const desired = new Map((props.widgets || []).map((w) => [w.id, w]))

  // Remove stale nodes
  current.forEach((node) => {
    const id = String(node.id || '')
    const cleanId = id.startsWith('modal-') ? id.substring(6) : id
    if (!desired.has(cleanId) && node.el) {
      grid.value?.removeWidget(node.el as HTMLElement, false, false)
    }
  })

  // Add / update nodes
  for (const w of props.widgets || []) {
    const modalId = `modal-${w.id}`
    const el = document.querySelector(`[gs-id="${modalId}"]`)
    if (!el) continue
    const opts = { id: modalId, x: w.x, y: w.y, w: w.width, h: w.height, autoPosition: false }
    const existing = current.get(modalId)
    if (existing) {
      if (existing.x !== w.x || existing.y !== w.y || existing.w !== w.width || existing.h !== w.height) {
        grid.value.update(el as HTMLElement, opts as any)
      }
    } else {
      grid.value.addWidget(el as HTMLElement, opts as any)
    }
  }

  grid.value.commit()
}

onMounted(async () => {
  initGrid()
  await loadWidgetsIntoGrid()
})

watch(
  () => props.widgets,
  async () => {
    await loadWidgetsIntoGrid()
  },
  { deep: true }
)

onBeforeUnmount(() => {
  grid.value?.destroy(false)
  grid.value = null
})
</script>

<style>
.grid-stack-modal {
  min-height: 600px;
  transition: transform 0.2s ease-out;
}
</style>


