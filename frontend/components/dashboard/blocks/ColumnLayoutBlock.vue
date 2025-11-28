<template>
  <div class="column-layout-block h-full w-full overflow-hidden">
    <div class="column-grid h-full" :style="gridStyle">
      <div 
        v-for="(column, index) in columns" 
        :key="index"
        class="column-item h-full overflow-hidden"
        :style="{ gridColumn: `span ${column.span}` }"
      >
        <slot :name="`column-${index}`" :column="column" :index="index" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

interface ColumnDef {
  span: number
  children?: any[]
}

const props = defineProps<{
  block: {
    columns?: ColumnDef[]
    view_overrides?: Record<string, any>
  }
  gap?: number
}>()

const columns = computed(() => props.block.columns || [])

const gridStyle = computed(() => {
  const gap = props.gap ?? 16 // Default 16px (1rem) gap
  return {
    display: 'grid',
    gridTemplateColumns: 'repeat(12, 1fr)',
    gap: `${gap}px`,
    height: '100%'
  }
})
</script>

<style scoped>
.column-layout-block {
  container-type: inline-size;
}

.column-grid {
  min-height: 0;
}

.column-item {
  min-width: 0;
  display: flex;
  flex-direction: column;
}

/* Responsive: stack columns on small screens */
@container (max-width: 600px) {
  .column-grid {
    grid-template-columns: 1fr !important;
  }
  
  .column-item {
    grid-column: span 1 !important;
  }
}
</style>

