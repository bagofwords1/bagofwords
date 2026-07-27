<template>
  <div class="mt-1">
    <!-- Status header (click to expand/collapse results) -->
    <Transition name="fade" appear>
      <div
        class="mb-2 flex items-center text-xs text-gray-500 dark:text-gray-400 cursor-pointer hover:text-gray-700 dark:hover:text-gray-300"
        @click="hasContent && toggleCollapsed()"
        :aria-expanded="!isCollapsed"
      >
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-magnifying-glass" class="w-3 h-3 me-1 text-gray-400 dark:text-gray-500" />
          <span>Searching&nbsp;</span>
          <Transition name="fade-in" mode="out-in">
            <span :key="queryLabel || ''">{{ queryLabel }}</span>
          </Transition>
          <span>…</span>
        </span>
        <span v-else class="text-gray-700 dark:text-gray-300 flex items-center min-w-0">
          <Icon
            v-if="hasContent"
            :name="isCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'"
            class="w-3 h-3 me-1 text-gray-400 dark:text-gray-500 rtl-flip flex-shrink-0"
          />
          <Icon name="heroicons-magnifying-glass" class="w-3 h-3 me-1 text-gray-400 dark:text-gray-500 flex-shrink-0" />
          <span class="align-middle flex-shrink-0">Searched&nbsp;</span>
          <span v-if="headerTables.length" class="flex items-center min-w-0 overflow-hidden">
            <span
              v-for="(item, idx) in headerTables"
              :key="idx"
              class="inline-flex items-center flex-shrink-0"
            >
              <DataSourceIcon :type="inferIconTypeFromItem(item)" class="h-2 me-1" :class="{ 'ms-1': idx > 0 }" />
              <span class="font-medium">{{ item.full_name || item.name || 'table' }}</span>
              <span v-if="idx < headerTables.length - 1 || headerExtraCount > 0">,</span>
            </span>
            <span v-if="headerExtraCount > 0" class="ms-1 text-gray-400 dark:text-gray-500 flex-shrink-0">+{{ headerExtraCount }} more</span>
          </span>
          <Transition v-else name="fade-in" mode="out-in">
            <span :key="queryLabel || ''" class="align-middle">{{ queryLabel }}</span>
          </Transition>
        </span>
      </div>
    </Transition>
    <!-- Preview of top tables (collapsed by default; click header to toggle) -->
    <Transition name="fade">
      <div v-if="!isCollapsed && topTables && topTables.length" class="text-xs text-gray-600 dark:text-gray-400">
        <ul class="ms-1 space-y-1 leading-snug">
          <li v-for="(item, idx) in topTables.slice(0, 10)" :key="idx">
            <!-- Header row -->
            <div
              class="flex items-center py-1 px-1 rounded cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
              @click="toggleItem(idx)"
              :aria-expanded="isExpanded(idx)"
            >
              <Icon :name="isExpanded(idx) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-400 dark:text-gray-500 me-1 rtl-flip" />
              <DataSourceIcon :type="inferIconTypeFromItem(item)" class="h-2 me-1" />
              <span v-if="item.connection_name" class="text-[9px] px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 me-1 flex-shrink-0 truncate max-w-[100px]">{{ item.connection_name }}</span>
              <div class="font-medium text-gray-700 dark:text-gray-300 truncate">
                {{ item.full_name || item.name || 'table' }}
              </div>
            </div>
            <!-- Detail row -->
            <Transition name="fade">
              <div v-if="isExpanded(idx)" class="ps-6 pe-1 pb-1">
                <!-- Columns -->
                <div v-if="(item.columns || []).length" class="text-gray-500 dark:text-gray-400 mb-1">
                  <table class="min-w-0 text-[11px]">
                    <thead class="text-gray-400 dark:text-gray-500">
                      <tr>
                        <th class="text-start pe-4 font-normal">Column</th>
                        <th class="text-start font-normal">Type</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="(col, cidx) in columnsHead(item)" :key="cidx">
                        <td class="pe-4 text-gray-600 dark:text-gray-400">{{ col.name }}</td>
                        <td class="text-gray-400 dark:text-gray-500">{{ col.dtype || 'any' }}</td>
                      </tr>
                      <tr v-if="columnsTruncated(item)">
                        <td colspan="2" class="text-gray-400 dark:text-gray-500">…</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <!-- Usage (always visible; shows placeholders when missing) -->
                <div class="text-gray-500 dark:text-gray-400 flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span class="inline-flex items-center gap-1" :class="{'text-gray-400 dark:text-gray-500': !hasUses(item)}">
                    <Icon name="heroicons-chart-bar" class="w-3 h-3" :class="hasUses(item) ? 'text-gray-400 dark:text-gray-500' : 'text-gray-300 dark:text-gray-600'" />
                    {{ usageCountLabel(item) }}
                  </span>
                  <span class="inline-flex items-center gap-1" :class="{'text-gray-400 dark:text-gray-500': !hasSuccess(item)}">
                    <Icon name="heroicons-check-circle" class="w-3 h-3" :class="hasSuccess(item) ? 'text-green-500' : 'text-gray-300 dark:text-gray-600'" />
                    {{ usageSuccessLabel(item) }}
                  </span>
                  <span class="inline-flex items-center gap-1 text-gray-400 dark:text-gray-500">
                    <Icon name="heroicons-clock" class="w-3 h-3" />
                    {{ lastUsedLabel(item) }}
                  </span>
                </div>
              </div>
            </Transition>
          </li>
          <!-- Related instructions (inside table list, after all tables) -->
          <li v-if="relatedInstructions.length">
            <div
              class="flex items-center py-1 px-1 rounded cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
              @click="showInstructions = !showInstructions"
            >
              <Icon :name="showInstructions ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-400 dark:text-gray-500 me-1 rtl-flip" />
              <Icon name="heroicons-cube" class="w-3 h-3 me-1 text-indigo-400" />
              <span class="text-gray-600 dark:text-gray-400">{{ relatedInstructions.length }} instruction{{ relatedInstructions.length !== 1 ? 's' : '' }} loaded</span>
            </div>
            <Transition name="fade">
              <div v-if="showInstructions" class="ps-6 pe-1 pb-1 space-y-0.5">
                <div v-for="ins in relatedInstructions" :key="ins.id"
                     class="flex items-center gap-2 text-gray-600 dark:text-gray-400 py-0.5 cursor-pointer hover:text-gray-900 dark:hover:text-white"
                     @click="emit('openInstruction', ins.id)">
                  <Icon name="heroicons-cube" class="w-3 h-3 text-indigo-400 flex-shrink-0" />
                  <span class="truncate">{{ ins.title || ins.text || 'Untitled' }}</span>
                  <span v-if="ins.category" class="text-[9px] px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 flex-shrink-0">{{ ins.category }}</span>
                </div>
              </div>
            </Transition>
          </li>
        </ul>
      </div>
    </Transition>
  </div>

</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import Spinner from '~/components/Spinner.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'

interface ToolExecution {
  id: string
  tool_name: string
  tool_action?: string
  status: string
  result_summary?: string
  result_json?: any
}

interface Props {
  toolExecution: ToolExecution
}

const props = defineProps<Props>()
const emit = defineEmits<{ (e: 'openInstruction', id: string): void }>()

const status = computed<string>(() => props.toolExecution?.status || '')

const queryLabel = computed<string>(() => {
  const rj = props.toolExecution?.result_json || {}
  // Prefer explicit search_query from result
  let q: any = rj.search_query
  // Fallback to original arguments sent to tool
  if (q == null) q = (props.toolExecution as any)?.arguments_json?.query
  if (Array.isArray(q)) return q.join(', ')
  if (typeof q === 'string') return q
  if (q && typeof q === 'object') return JSON.stringify(q)
  // Fallback to summary parsing if present
  const sum = props.toolExecution?.result_summary || ''
  const m = sum.match(/^Searching\s+(.+?)…?$/)
  return m ? m[1] : 'tables'
})

// Extract top tables from backend (lightweight preview)
const topTables = computed<any[]>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  const tt = Array.isArray(rj.top_tables) ? rj.top_tables : []
  return tt
})

const relatedInstructions = computed<any[]>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  return Array.isArray(rj.related_instructions) ? rj.related_instructions : []
})

const showInstructions = ref(false)

// Whole result list collapsed by default; header shows table names + icons
const isCollapsed = ref(true)
function toggleCollapsed() {
  isCollapsed.value = !isCollapsed.value
}

const hasContent = computed<boolean>(() => topTables.value.length > 0 || relatedInstructions.value.length > 0)

// Deduped tables for the compact header line
const HEADER_MAX_TABLES = 6
const dedupedTables = computed<any[]>(() => {
  const seen = new Set<string>()
  const out: any[] = []
  for (const item of topTables.value) {
    const key = `${item?.connection_name || ''}:${item?.full_name || item?.name || ''}`
    if (seen.has(key)) continue
    seen.add(key)
    out.push(item)
  }
  return out
})
const headerTables = computed<any[]>(() => dedupedTables.value.slice(0, HEADER_MAX_TABLES))
const headerExtraCount = computed<number>(() => Math.max(0, dedupedTables.value.length - HEADER_MAX_TABLES))

const expandedItems = ref<Set<number>>(new Set())
function toggleItem(index: number) {
  if (expandedItems.value.has(index)) {
    expandedItems.value.delete(index)
  } else {
    expandedItems.value.add(index)
  }
}
function isExpanded(index: number): boolean {
  return expandedItems.value.has(index)
}

function inferIconTypeFromItem(item: any): string {
  try {
    const t = String(item?.data_source_type || '').toLowerCase()
    return t || 'resource'
  } catch {
    return 'resource'
  }
}

function columnsHead(item: any, max = 8): any[] {
  try {
    const cols = Array.isArray(item?.columns) ? item.columns : []
    return cols.slice(0, max)
  } catch {
    return []
  }
}

function columnsTruncated(item: any, max = 8): boolean {
  try {
    const cols = Array.isArray(item?.columns) ? item.columns : []
    return cols.length > max
  } catch {
    return false
  }
}

function formatInt(n: number): string {
  try {
    return new Intl.NumberFormat().format(Number(n || 0))
  } catch {
    return String(n || 0)
  }
}

function formatPct(v: number): string {
  try {
    const pct = Number(v || 0) * 100
    return `${pct.toFixed(0)}%`
  } catch {
    return ''
  }
}

function timeAgo(iso: string): string {
  try {
    const d = new Date(iso)
    const diffMs = Date.now() - d.getTime()
    const sec = Math.max(1, Math.floor(diffMs / 1000))
    const min = Math.floor(sec / 60)
    const hr = Math.floor(min / 60)
    const day = Math.floor(hr / 24)
    if (day > 0) return `${day}d ago`
    if (hr > 0) return `${hr}h ago`
    if (min > 0) return `${min}m ago`
    return `${sec}s ago`
  } catch {
    return ''
  }
}

function hasUses(item: any): boolean {
  try {
    return item?.usage?.usage_count != null
  } catch {
    return false
  }
}

function hasSuccess(item: any): boolean {
  try {
    return item?.usage?.success_rate != null
  } catch {
    return false
  }
}

function usageCountLabel(item: any): string {
  try {
    const n = item?.usage?.usage_count
    return `${formatInt(n || 0)} uses`
  } catch {
    return '0 uses'
  }
}

function usageSuccessLabel(item: any): string {
  try {
    const r = item?.usage?.success_rate
    return r != null ? `${formatPct(r)} success` : '—'
  } catch {
    return '—'
  }
}

function lastUsedLabel(item: any): string {
  try {
    const d = item?.usage?.last_used_at
    return d ? timeAgo(d) : '—'
  } catch {
    return '—'
  }
}
</script>

<style scoped>
.tool-shimmer {
  animation: shimmer 1.6s linear infinite;
  background: linear-gradient(90deg, rgba(0,0,0,0) 0%, rgba(160,160,160,0.15) 50%, rgba(0,0,0,0) 100%);
  background-size: 300% 100%;
  background-clip: text;
}

@keyframes shimmer {
  0% { background-position: 0% 0; }
  100% { background-position: 100% 0; }
}

/* Fade transition for initial appear and toggles */
.fade-enter-active, .fade-leave-active {
  transition: opacity 0.25s ease, transform 0.25s ease;
}
.fade-enter-from, .fade-leave-to {
  opacity: 0;
  transform: translateY(2px);
}
</style>

