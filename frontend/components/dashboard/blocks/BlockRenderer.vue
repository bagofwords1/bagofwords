<template>
  <div class="block-renderer h-full w-full" :class="blockClass">
    <!-- Text Block (inline content) -->
    <template v-if="blockType === 'text'">
      <TextBlock 
        :block="block" 
        :themeName="themeName" 
        :reportOverrides="reportOverrides" 
      />
    </template>

    <!-- Visualization Block -->
    <template v-else-if="blockType === 'visualization'">
      <slot name="visualization" :block="block">
        <!-- Default: render RegularWidgetView if no slot provided -->
        <RegularWidgetView
          v-if="widget"
          :widget="widget"
          :themeName="themeName"
          :reportOverrides="reportOverrides"
        />
        <div v-else class="flex items-center justify-center h-full text-gray-400 text-sm">
          Loading visualization...
        </div>
      </slot>
    </template>

    <!-- Card Block -->
    <template v-else-if="blockType === 'card'">
      <CardBlock :block="block" :themeName="themeName" :reportOverrides="reportOverrides" :contentIsMetricCard="cardContainsMetricCard">
        <!-- Single child: fill available space -->
        <template v-if="children.length === 1">
          <div class="h-full w-full">
            <BlockRenderer
              :block="children[0]"
              :widget="getWidgetForBlock(children[0])"
              :depth="depth + 1"
              :themeName="themeName"
              :reportOverrides="reportOverrides"
              :getWidgetForBlock="props.getWidgetForBlock"
            >
              <template #visualization="{ block: childBlock }">
                <slot name="visualization" :block="childBlock" />
              </template>
            </BlockRenderer>
          </div>
        </template>
        <!-- Multiple children: use absolute positioning -->
        <template v-else>
          <div class="card-children h-full" :style="childrenContainerStyle">
            <template v-for="(child, idx) in children" :key="childKey(child, idx)">
              <div 
                class="card-child"
                :style="childPositionStyle(child)"
              >
                <BlockRenderer
                  :block="child"
                  :widget="getWidgetForBlock(child)"
                  :depth="depth + 1"
                  :themeName="themeName"
                  :reportOverrides="reportOverrides"
                  :getWidgetForBlock="props.getWidgetForBlock"
                >
                  <template #visualization="{ block: childBlock }">
                    <slot name="visualization" :block="childBlock" />
                  </template>
                </BlockRenderer>
              </div>
            </template>
          </div>
        </template>
      </CardBlock>
    </template>

    <!-- Column Layout Block -->
    <template v-else-if="blockType === 'column_layout'">
      <ColumnLayoutBlock :block="block">
        <template v-for="(column, colIdx) in columns" :key="colIdx" #[`column-${colIdx}`]>
          <div class="column-children h-full flex flex-col gap-4">
            <template v-for="(child, childIdx) in column.children || []" :key="childKey(child, childIdx)">
              <div 
                class="column-child flex-shrink-0"
                :style="childHeightStyle(child)"
              >
                <BlockRenderer
                  :block="child"
                  :widget="getWidgetForBlock(child)"
                  :depth="depth + 1"
                  :themeName="themeName"
                  :reportOverrides="reportOverrides"
                  :getWidgetForBlock="props.getWidgetForBlock"
                >
                  <template #visualization="{ block: childBlock }">
                    <slot name="visualization" :block="childBlock" />
                  </template>
                </BlockRenderer>
              </div>
            </template>
          </div>
        </template>
      </ColumnLayoutBlock>
    </template>

    <!-- Text Widget (legacy with text_widget_id) -->
    <template v-else-if="blockType === 'text_widget' && block.text_widget_id">
      <slot name="text_widget" :block="block">
        <TextWidgetView
          v-if="widget"
          :widget="widget"
          :themeName="themeName"
          :reportOverrides="reportOverrides"
        />
      </slot>
    </template>

    <!-- Text Widget with inline content (fallback) -->
    <template v-else-if="blockType === 'text_widget' && block.content">
      <TextBlock 
        :block="block" 
        :themeName="themeName" 
        :reportOverrides="reportOverrides" 
      />
    </template>

    <!-- Unknown block type -->
    <template v-else>
      <div class="unknown-block p-4 text-gray-400 text-sm">
        Unknown block type: {{ blockType }}
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent } from 'vue'

// Lazy load block components to avoid circular imports
const TextBlock = defineAsyncComponent(() => import('./TextBlock.vue'))
const CardBlock = defineAsyncComponent(() => import('./CardBlock.vue'))
const ColumnLayoutBlock = defineAsyncComponent(() => import('./ColumnLayoutBlock.vue'))
const RegularWidgetView = defineAsyncComponent(() => import('../regular/RegularWidgetView.vue'))
const TextWidgetView = defineAsyncComponent(() => import('../text/TextWidgetView.vue'))

interface Block {
  type: string
  x?: number
  y?: number
  width?: number
  height?: number
  visualization_id?: string
  text_widget_id?: string
  content?: string
  variant?: string
  children?: Block[]
  columns?: Array<{ span: number; children?: Block[] }>
  chrome?: Record<string, any>
  view_overrides?: Record<string, any>
}

const props = defineProps<{
  block: Block
  widget?: any
  depth?: number
  themeName?: string | null
  reportOverrides?: Record<string, any> | null
  getWidgetForBlock?: (block: Block) => any
}>()

// Block type detection
const blockType = computed(() => props.block.type || 'unknown')

// Children for cards
const children = computed(() => props.block.children || [])

// Columns for column_layout
const columns = computed(() => props.block.columns || [])

// Depth for styling
const depth = computed(() => props.depth ?? 0)

// Block-level styling class
const blockClass = computed(() => {
  if (depth.value > 0) return 'block-renderer--nested'
  return ''
})

// Check if card contains a metric_card visualization (to hide duplicate header)
const cardContainsMetricCard = computed(() => {
  for (const child of children.value) {
    if (child.type === 'visualization' && child.visualization_id) {
      const widget = getWidgetForBlock(child)
      const viewType = widget?.view?.view?.type || widget?.view?.type
      if (viewType === 'metric_card' || viewType === 'count') {
        return true
      }
    }
  }
  return false
})

// Helper to get widget data for a child block
function getWidgetForBlock(childBlock: Block): any {
  if (props.getWidgetForBlock) {
    return props.getWidgetForBlock(childBlock)
  }
  return undefined
}

// Generate unique key for children
function childKey(child: Block, index: number): string {
  if (child.visualization_id) return `viz-${child.visualization_id}`
  if (child.text_widget_id) return `tw-${child.text_widget_id}`
  if (child.content) return `text-${index}-${child.content.substring(0, 20)}`
  return `child-${index}`
}

// Grid cell height in pixels (should match GridStack config)
const CELL_HEIGHT = 40

// Container style for card children (relative positioning grid)
const childrenContainerStyle = computed(() => {
  if (!children.value.length) return {}
  
  // Calculate total height from children
  let maxY = 0
  for (const child of children.value) {
    const endY = (child.y || 0) + (child.height || 1)
    if (endY > maxY) maxY = endY
  }
  
  return {
    position: 'relative' as const,
    minHeight: `${maxY * CELL_HEIGHT}px`
  }
})

// Position style for each child within a card (absolute positioning)
function childPositionStyle(child: Block) {
  return {
    position: 'absolute' as const,
    left: `${((child.x || 0) / 12) * 100}%`,
    top: `${(child.y || 0) * CELL_HEIGHT}px`,
    width: `${((child.width || 12) / 12) * 100}%`,
    height: `${(child.height || 1) * CELL_HEIGHT}px`
  }
}

// Height style for column children (stacked vertically)
function childHeightStyle(child: Block) {
  const h = child.height || 6
  return {
    height: `${h * CELL_HEIGHT}px`,
    minHeight: `${h * CELL_HEIGHT}px`
  }
}
</script>

<style scoped>
.block-renderer {
  min-height: 0;
  overflow: hidden;
}

.block-renderer--nested {
  /* Nested blocks may have different styling */
}

.card-children {
  width: 100%;
}

.card-child {
  padding: 4px;
}

.column-children {
  padding: 4px;
}

.column-child {
  overflow: hidden;
}

.unknown-block {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  border: 1px dashed #ccc;
  border-radius: 8px;
}
</style>

