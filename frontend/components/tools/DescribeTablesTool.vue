<template>
  <div class="mt-1">
    <!-- Status header -->
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700">
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-magnifying-glass" class="w-3 h-3 mr-1 text-gray-400" />
          Searching {{ queryLabel }}…
        </span>
        <span v-else class="text-gray-700 flex items-center">
          <Icon name="heroicons-magnifying-glass" class="w-3 h-3 mr-1 text-gray-400" />
          <span class="align-middle">Searched {{ queryLabel }}</span>
        </span>
      </div>
    </Transition>
    <!-- Preview of top tables (click to toggle details) -->
    <Transition name="fade" appear>
      <div v-if="topTables && topTables.length" class="text-xs text-gray-600">
        <ul class="ml-1 space-y-1 leading-snug">
          <li v-for="(item, idx) in topTables.slice(0, 10)" :key="idx">
            <!-- Header row -->
            <div
              class="flex items-center py-1 px-1 rounded cursor-pointer hover:bg-gray-50"
              @click="toggleItem(idx)"
              :aria-expanded="isExpanded(idx)"
            >
              <Icon :name="isExpanded(idx) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-400 mr-1" />
              <DataSourceIcon :type="inferIconTypeFromItem(item)" class="h-3 mr-2" />
              <div class="font-medium text-gray-700 truncate">
                {{ item.full_name || item.name || 'table' }}
              </div>
            </div>
            <!-- Detail row -->
            <Transition name="fade">
              <div v-if="isExpanded(idx)" class="pl-6 pr-1 pb-1">
                <!-- Columns -->
                <div v-if="(item.columns || []).length" class="text-gray-500 mb-1">
                  <table class="min-w-0 text-[11px]">
                    <thead class="text-gray-400">
                      <tr>
                        <th class="text-left pr-4 font-normal">Column</th>
                        <th class="text-left font-normal">Type</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="(col, cidx) in columnsHead(item)" :key="cidx">
                        <td class="pr-4 text-gray-600">{{ col.name }}</td>
                        <td class="text-gray-400">{{ col.dtype || 'any' }}</td>
                      </tr>
                      <tr v-if="columnsTruncated(item)">
                        <td colspan="2" class="text-gray-400">…</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <!-- Usage (always visible; shows placeholders when missing) -->
                <div class="text-gray-500 flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span class="inline-flex items-center gap-1" :class="{'text-gray-400': !hasUses(item)}">
                    <Icon name="heroicons-chart-bar" class="w-3 h-3" :class="hasUses(item) ? 'text-gray-400' : 'text-gray-300'" />
                    {{ usageCountLabel(item) }}
                  </span>
                  <span class="inline-flex items-center gap-1" :class="{'text-gray-400': !hasSuccess(item)}">
                    <Icon name="heroicons-check-circle" class="w-3 h-3" :class="hasSuccess(item) ? 'text-green-500' : 'text-gray-300'" />
                    {{ usageSuccessLabel(item) }}
                  </span>
                  <span class="inline-flex items-center gap-1 text-gray-400">
                    <Icon name="heroicons-clock" class="w-3 h-3" />
                    {{ lastUsedLabel(item) }}
                  </span>
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


