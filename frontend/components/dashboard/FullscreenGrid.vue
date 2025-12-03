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
          <!-- Legacy text widget (with DB reference, no inline content) -->
          <template v-if="widget.type === 'text' && widget.id && !widget.content">
            <TextWidgetView
              :widget="widget"
              :themeName="props.themeName"
              :reportOverrides="props.reportOverrides"
            />
          </template>
          <!-- Inline text block (AI generated, has content) -->
          <template v-else-if="widget.type === 'text' && widget.content">
            <TextBlock
              :block="widget"
              :themeName="props.themeName"
              :reportOverrides="props.reportOverrides"
            />
          </template>
          <!-- Card block with children -->
          <template v-else-if="widget.type === 'card'">
            <CardBlock
              :block="widget"
              :themeName="props.themeName"
              :reportOverrides="props.reportOverrides"
              :contentIsMetricCard="props.cardContainsMetricCard?.(widget) || false"
            >
              <BlockRenderer
                v-for="(child, idx) in widget.children || []"
                :key="`card-child-${idx}-${child.visualization_id || child.content?.substring(0,10) || idx}`"
                :block="child"
                :widget="props.getWidgetForBlock?.(child)"
                :themeName="props.themeName"
                :reportOverrides="props.reportOverrides"
                :getWidgetForBlock="props.getWidgetForBlock"
                :reportId="props.report?.id"
              />
            </CardBlock>
          </template>
          <!-- Column layout block -->
          <template v-else-if="widget.type === 'column_layout'">
            <ColumnLayoutBlock :block="widget">
              <template v-for="(col, colIdx) in widget.columns || []" :key="colIdx" #[`column-${colIdx}`]>
                <div class="flex flex-col gap-4 h-full">
                  <BlockRenderer
                    v-for="(child, childIdx) in col.children || []"
                    :key="`col-${colIdx}-child-${childIdx}`"
                    :block="child"
                    :widget="props.getWidgetForBlock?.(child)"
                    :themeName="props.themeName"
                    :reportOverrides="props.reportOverrides"
                    :getWidgetForBlock="props.getWidgetForBlock"
                    :reportId="props.report?.id"
                    class="flex-shrink-0"
                    :style="{ height: `${(child.height || 6) * 40}px` }"
                  />
                </div>
              </template>
            </ColumnLayoutBlock>
          </template>
          <!-- Regular visualization -->
          <template v-else>
            <RegularWidgetView
              :widget="widget"
              :themeName="props.themeName"
              :reportOverrides="props.reportOverrides"
              :reportId="props.report?.id"
            />
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
import TextBlock from '@/components/dashboard/blocks/TextBlock.vue'
import CardBlock from '@/components/dashboard/blocks/CardBlock.vue'
import ColumnLayoutBlock from '@/components/dashboard/blocks/ColumnLayoutBlock.vue'
import BlockRenderer from '@/components/dashboard/blocks/BlockRenderer.vue'

const props = defineProps<{
  widgets: any[]
  report: any
  themeName: string | null
  reportOverrides?: any
  tokens?: any
  itemStyle?: any
  zoom?: number
  getWidgetForBlock?: (block: any) => any
  cardContainsMetricCard?: (widget: any) => boolean
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


