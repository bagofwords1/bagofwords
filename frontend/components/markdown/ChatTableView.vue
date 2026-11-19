<template>
  <div class="chat-table-view" :class="{ 'is-expanded': expanded }">
    <div ref="viewport" class="chat-table-scroll" tabindex="0" role="region" :aria-label="t('markdownTable.title')" @scroll.passive="updateEdges">
      <table ref="table" class="chat-table" :aria-busy="!!node.loading">
        <thead>
          <tr>
            <th v-for="(cell, index) in node.header?.cells || []" :key="index" :style="alignment(cell)">
              <MarkdownRender :nodes="cell.children" :custom-id="customId" :index-key="`${indexKey}-heading-${index}`" :typewriter="false" />
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, rowIndex) in node.rows || []" :key="rowIndex">
            <td v-for="(cell, columnIndex) in row.cells" :key="columnIndex" :style="alignment(cell)" :class="{ 'is-scalar': isScalar(cell) }">
              <div class="chat-table-cell">
                <MarkdownRender :nodes="cell.children" :custom-id="customId" :index-key="`${indexKey}-${rowIndex}-${columnIndex}`" :typewriter="false" />
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-show="leftOverflow" class="chat-table-fade fade-left" aria-hidden="true" />
    <div v-show="rightOverflow" class="chat-table-fade fade-right" aria-hidden="true" />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { MarkdownRender } from 'markstream-vue'
const props = defineProps<{ node: any; customId?: string; indexKey?: string | number; expanded?: boolean }>()
const { t } = useI18n()
const viewport = ref<HTMLElement | null>(null)
const table = ref<HTMLTableElement | null>(null)
const leftOverflow = ref(false)
const rightOverflow = ref(false)
let observer: ResizeObserver | undefined
function updateEdges() {
  if (!viewport.value || !table.value) return
  const outer = viewport.value.getBoundingClientRect()
  const inner = table.value.getBoundingClientRect()
  leftOverflow.value = inner.left < outer.left - 1
  rightOverflow.value = inner.right > outer.right + 1
}
function cellText(cell: any): string {
  return String(cell.raw || cell.children?.map((child: any) => child.content || child.raw || '').join('') || '').trim()
}
function isNumber(cell: any): boolean {
  return /^[\p{Sc}\s+-]*\d[\d\s,.]*(?:%|\s+[A-Z]{3})?$/u.test(cellText(cell))
}
function isScalar(cell: any): boolean {
  return isNumber(cell) || /^\d{4}-\d{2}-\d{2}$/.test(cellText(cell)) || /^(?:[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})$/.test(cellText(cell))
}
function alignment(cell: any) {
  return { textAlign: cell.align || (isNumber(cell) ? 'end' : 'start') } as any
}
const revision = computed(() => props.node.raw || JSON.stringify(props.node.rows || []))
watch(revision, () => nextTick(updateEdges))
onMounted(() => {
  observer = new ResizeObserver(updateEdges)
  if (viewport.value) observer.observe(viewport.value)
  if (table.value) observer.observe(table.value)
  updateEdges()
})
onBeforeUnmount(() => observer?.disconnect())
</script>

<style scoped>
.chat-table-view { position: relative; min-width: 0; max-width: 100%; }
.chat-table-scroll { overflow: auto; max-height: 25rem; scrollbar-width: thin; }
.is-expanded .chat-table-scroll { max-height: 75vh; }
.chat-table-view .chat-table {
  width: max-content !important;
  min-width: 100%;
  max-width: none !important;
  table-layout: auto !important;
  border-collapse: separate !important;
  border-spacing: 0;
  margin: 0 !important;
  border: 0 !important;
  font-size: var(--bow-chat-font-size);
  line-height: var(--bow-chat-line-height);
}
.chat-table-view .chat-table :is(th, td) {
  border: 0 !important;
  border-bottom: 1px solid rgb(var(--bow-line-rgb)) !important;
  padding: 0.625rem 1rem !important;
  vertical-align: top;
  background: rgb(var(--bow-bg-rgb));
  color: rgb(var(--bow-chat-ink-rgb));
  font-size: inherit !important;
  line-height: inherit;
  word-break: normal;
}
.chat-table-view .chat-table th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: rgb(var(--bow-bg-rgb));
  color: rgb(var(--bow-ink-muted-rgb));
  font-weight: 600;
  white-space: nowrap;
}
.chat-table-view .chat-table td { background: transparent; }
.chat-table-view .chat-table tbody tr:last-child td { border-bottom: 0 !important; }
.chat-table-cell { min-width: 6rem; max-width: 22rem; white-space: normal; overflow-wrap: anywhere; }
.chat-table td.is-scalar .chat-table-cell { white-space: nowrap; font-variant-numeric: tabular-nums; }
.chat-table-view :deep(.markstream-vue) { font-size: inherit; line-height: inherit; text-align: inherit; }
.chat-table-view :deep(.paragraph-node) { margin: 0; }
.chat-table-fade { position: absolute; top: 0; bottom: 0.75rem; width: 1.5rem; pointer-events: none; }
.fade-left { left: 0; background: linear-gradient(to right, rgb(var(--bow-bg-rgb)), transparent); }
.fade-right { right: 0; background: linear-gradient(to left, rgb(var(--bow-bg-rgb)), transparent); }
</style>
